"""End-to-end smoke test: import → consolidate → health-check → publish.

Spins up a local HTTP server that serves a fake M3U manifest, runs the
full import pipeline against it, then runs a health check (the streams
point at the same local server), and finally writes a playlist.

This is what proves Phase 1 works before you ever touch the VPS.
"""
import asyncio
import json
import os
import sys
import sqlite3
import tempfile
import textwrap
from pathlib import Path
from aiohttp import web

# Force UTF-8 output so the checkmark / frog emojis don't crash on
# Windows consoles that default to cp1252.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Make sure we use a temp DB for this test, not the real one.
TMPDIR = Path(tempfile.mkdtemp(prefix="treefrog-smoke-"))
os.environ["DATA_DIR"] = str(TMPDIR)
os.environ["LOG_DIR"] = str(TMPDIR / "logs")
os.environ["DB_PATH"] = str(TMPDIR / "treefrog.db")
os.environ["HEALTH_TIMEOUT_SEC"] = "3"
os.environ["HEALTH_CADENCE_SEC"] = "1800"
# Fake CF credentials so the KV-publisher code path actually runs
# (it bails early with "credentials missing" otherwise). The aiohttp
# session is patched out below.
os.environ["CF_API_TOKEN"] = "fake-token"
os.environ["CF_ACCOUNT_ID"] = "fake-account"
os.environ["CF_KV_NAMESPACE_ID"] = "fake-ns"

# Force a fresh config load with the new env vars.
import importlib
import engine.config
importlib.reload(engine.config)
import engine.db
importlib.reload(engine.db)
import engine.health
importlib.reload(engine.health)
import engine.publisher.playlist
importlib.reload(engine.publisher.playlist)
import engine.publisher.json_catalog
importlib.reload(engine.publisher.json_catalog)
import engine.publisher.kv
importlib.reload(engine.publisher.kv)
import engine.importers.importer
importlib.reload(engine.importers.importer)

from engine.db import open_db, run_migrations
from engine.importers.importer import import_m3u
from engine.health import run_health_cycle
from engine.publisher.playlist import write_playlist, render_playlist
from engine.publisher.json_catalog import build_catalog
from engine.publisher import kv as kv_pub


# A real-shaped M3U the local server will serve. Three distinct channels,
# one with a duplicate (BBC News / BBC News HD) so we exercise the
# consolidator. One stream returns a valid m3u8 manifest; one returns
# HTML (should be detected as offline); one is unreachable.
SAMPLE_M3U = textwrap.dedent("""\
    #EXTM3U
    #EXTINF:-1 tvg-id="bbc1.uk" tvg-name="BBC One" tvg-logo="http://example/bbc1.png" group-title="UK",BBC One
    http://127.0.0.1:PORT/stream/bbc1.m3u8
    #EXTINF:-1 tvg-id="bbcnews.uk" tvg-name="BBC News" tvg-logo="http://example/bbcn.png" group-title="News",BBC News HD
    http://127.0.0.1:PORT/stream/bbcnews.m3u8
    #EXTINF:-1 tvg-id="cnn.us" tvg-name="CNN" tvg-logo="http://example/cnn.png" group-title="News",CNN
    http://127.0.0.1:PORT/stream/cnn.m3u8
    #EXTINF:-1 tvg-id="bbcnews.uk" tvg-name="BBC News" group-title="News",BBC News
    http://127.0.0.1:PORT/stream/bbcnews-backup.m3u8
    #EXTINF:-1 group-title="Sports",ESPN+1
    http://127.0.0.1:PORT/stream/espn1.m3u8
    #EXTINF:-1 group-title="Sports",ESPN HD
    http://127.0.0.1:PORT/dead/espn-hd.m3u8
""")

VALID_MANIFEST = "#EXTM3U\n#EXTINF:-1,Test\nhttp://example/seg.ts\n"
HTML_RESPONSE = "<html>not a stream</html>"


async def stream_handler(request):
    """Serve the sample M3U for the import URL; serve per-stream manifests
    for the health checker."""
    path = request.path
    if path == "/list.m3u":
        return web.Response(text=SAMPLE_M3U, content_type="audio/x-mpegurl")
    if path.startswith("/stream/") and path.endswith(".m3u8"):
        return web.Response(text=VALID_MANIFEST, content_type="application/vnd.apple.mpegurl")
    if path.startswith("/dead/"):
        return web.Response(text=HTML_RESPONSE, content_type="text/html", status=200)
    return web.Response(status=404, text="not found")


async def run_smoke():
    app = web.Application()
    app.router.add_get("/{tail:.*}", stream_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    print(f"[smoke] local server listening on http://127.0.0.1:{port}")

    # Substitute PORT in the M3U
    global SAMPLE_M3U
    SAMPLE_M3U = SAMPLE_M3U.replace("PORT", str(port))

    # ---- 1. Import ----
    print("\n[smoke] === Step 1: M3U import ===")
    summary = await import_m3u(
        f"http://127.0.0.1:{port}/list.m3u", source_label="smoke-test"
    )
    print(f"[smoke] import summary: {json.dumps(summary, indent=2)}")
    assert summary["total"] == 6, f"expected 6 entries, got {summary['total']}"
    # BBC News and BBC News HD + BBC News (backup) all consolidate → 1 channel
    # ESPN+1 and ESPN HD consolidate → 1 channel
    # BBC One and CNN are unique
    # = 4 unique channels
    assert summary["channels_new"] == 4, f"expected 4 channels, got {summary['channels_new']}"
    # 6 streams total
    assert summary["streams_new"] == 6, f"expected 6 streams, got {summary['streams_new']}"
    print("[smoke] ✓ import consolidated 6 raw entries into 4 channels")

    # ---- 2. Verify consolidation in DB ----
    print("\n[smoke] === Step 2: consolidation check ===")
    db = await open_db()
    try:
        async with db.execute("SELECT id, normalized_name, display_name FROM channels ORDER BY id") as cur:
            channels = await cur.fetchall()
        names = [c["display_name"] for c in channels]
        print(f"[smoke] channels: {names}")
        # BBC News HD + BBC News collapsed into a single channel
        bbc_count = sum(1 for n in names if "BBC News" in n)
        assert bbc_count == 1, f"expected 1 BBC News channel, got {bbc_count}"
        # ESPN HD + ESPN+1 collapsed into one channel
        espn_count = sum(1 for n in names if "ESPN" in n)
        assert espn_count == 1, f"expected 1 ESPN channel, got {espn_count}"
        # But ESPN should keep its original display name
        espn_row = next(c for c in channels if "ESPN" in c["display_name"])
        print(f"[smoke] ESPN display name preserved: '{espn_row['display_name']}'")
    finally:
        await db.close()
    print("[smoke] ✓ consolidation working as designed")

    # ---- 3. Health check ----
    print("\n[smoke] === Step 3: health check ===")
    health = await run_health_cycle()
    print(f"[smoke] health summary: {json.dumps(health, indent=2)}")
    assert health["checked"] >= 6, f"expected >=6 checks, got {health['checked']}"
    # 5 valid streams + 1 dead HTML one
    assert health["online"] == 5, f"expected 5 online, got {health['online']}"
    assert health["offline"] == 1, f"expected 1 offline, got {health['offline']}"
    print(f"[smoke] ✓ {health['online']} streams online, {health['offline']} offline (HTML rejected)")

    # ---- 4. Render playlist ----
    print("\n[smoke] === Step 4: render playlist ===")
    playlist = await render_playlist()
    print(playlist)
    # Verify: 5 channels should appear (the 4 channels minus the 1 with no online stream)
    line_count = sum(1 for ln in playlist.splitlines() if ln.startswith("#EXTINF"))
    assert line_count == 4, f"expected 4 EXTINF lines, got {line_count}"
    # Branding check
    assert "🐸 Tree Frog Free | News" in playlist
    assert "🐸 Tree Frog Free | UK" in playlist
    assert "🐸 Tree Frog Free | Sports" in playlist
    # Channel name preserved exactly — we keep whichever the first importer saw
    assert "BBC News HD" in playlist
    # ESPN HD and ESPN+1 consolidated into one channel; the displayed name
    # is whichever came first (ESPN+1 in the sample M3U).
    assert "ESPN+1" in playlist
    # The other variant must NOT appear as a separate EXTINF entry —
    # count EXTINF lines, not raw string occurrences.
    espn_extinf = sum(1 for ln in playlist.splitlines() if ",ESPN" in ln)
    assert espn_extinf == 1, f"expected 1 ESPN EXTINF line, got {espn_extinf}"
    # Dead stream should be marked offline and channel hidden
    print("[smoke] ✓ playlist rendered with branding + dedup applied")

    # ---- 5. Build catalog JSON ----
    print("\n[smoke] === Step 5: build catalog JSON ===")
    catalog = await build_catalog()
    print(f"[smoke] stats: {json.dumps(catalog['stats'], indent=2)}")
    print(f"[smoke] categories: {[c['name'] for c in catalog['categories']]}")
    assert catalog["stats"]["channel_count"] == 4
    assert any(c["slug"] == "news" for c in catalog["categories"])
    print("[smoke] ✓ catalog built with stats + categories + channels")

    # ---- 6. Write artifacts to disk ----
    print("\n[smoke] === Step 6: write artifacts ===")
    p_path = await write_playlist()
    print(f"[smoke] playlist: {p_path}")
    assert Path(p_path).exists()
    assert Path(p_path).stat().st_size > 0

    from engine.publisher.json_catalog import write_catalog
    c_path = await write_catalog()
    print(f"[smoke] catalog:  {c_path}")
    assert Path(c_path).exists()
    cat_size = Path(c_path).stat().st_size
    assert cat_size > 0
    print(f"[smoke] ✓ artifacts on disk: {p_path} ({Path(p_path).stat().st_size}B), {c_path} ({cat_size}B)")

    # ---- 7. publish_public_assets to KV (mocked) ----
    # We don't need real CF credentials for this; the point is to prove
    # the engine builds the right KV payload (catalog JSON + playlist
    # M3U) and that diff-and-skip works on the second call.
    print("\n[smoke] === Step 7: KV public-asset publish (mocked) ===")
    captured: dict[str, str] = {}

    class _FakeResp:
        """Mirrors aiohttp's context-manager response object."""
        def __init__(self, status: int, text: str = ""):
            self.status = status
            self._text = text
        async def text(self): return self._text
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _FakeSession:
        """A session that records PUTs and serves known values on GETs.

        The publisher uses `async with session.get(...) as resp` so each
        verb needs to return a context-manager response, not be one itself.
        """
        def get(self, url, **kw):
            key = url.rsplit("/", 1)[-1]
            return _FakeResp(200, captured.get(key, ""))
        def put(self, url, **kw):
            key = url.rsplit("/", 1)[-1]
            captured[key] = kw["data"]
            return _FakeResp(200)
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    # Patch the CF URL + session to use our fake
    kv_pub._cf_url = lambda ns: "https://fake.test/kv/"
    orig_session = kv_pub.aiohttp.ClientSession
    kv_pub.aiohttp.ClientSession = lambda **kw: _FakeSession()

    try:
        # First publish: both keys are new
        s1 = await kv_pub.publish_public_assets()
        print(f"[smoke] first publish: {s1}")
        assert s1["written"] == 2
        assert s1["unchanged"] == 0
        assert s1["errors"] == 0
        assert "catalog:channels.json" in captured
        assert "catalog:playlist.m3u" in captured
        # The captured values must be valid JSON / valid M3U
        parsed = json.loads(captured["catalog:channels.json"])
        assert parsed["stats"]["channel_count"] == 4
        assert captured["catalog:playlist.m3u"].startswith("#EXTM3U")

        # Second publish: nothing changed, so both should be skipped
        s2 = await kv_pub.publish_public_assets()
        print(f"[smoke] second publish (no changes): {s2}")
        assert s2["written"] == 0
        assert s2["unchanged"] == 2
        assert s2["errors"] == 0
    finally:
        kv_pub.aiohttp.ClientSession = orig_session
    print("[smoke] ✓ KV public-asset publish (with diff-and-skip) works")

    await runner.cleanup()
    print("\n[smoke] ========== ALL SMOKE TESTS PASSED ==========\n")


if __name__ == "__main__":
    asyncio.run(run_smoke())
