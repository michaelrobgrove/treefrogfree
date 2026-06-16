"""Smoke test for the CLI dispatch layer (engine/__main__.py).

Regression test for the bug where `python -m engine` (no subcommand)
crashed with "the following arguments are required: cmd", and
`python -m engine serve` crashed with "asyncio.run() cannot be called
from a running event loop" because _cmd_serve called the sync
scheduler.main() which itself called asyncio.run().

We don't actually run the long-lived scheduler here (it would never
exit). Instead we verify:
  1. The argparse subparser is wired correctly (each known subcommand
     is recognized).
  2. The serve subcommand's handler is an async coroutine function
     that can be awaited without re-entering the event loop.
"""
import asyncio
import pytest
import sys
import tempfile
import os
from pathlib import Path

# Force a temp DB so the test doesn't touch the real one.
TMPDIR = Path(tempfile.mkdtemp(prefix="treefrog-cli-"))
os.environ["DATA_DIR"] = str(TMPDIR)
os.environ["LOG_DIR"] = str(TMPDIR / "logs")
os.environ["DB_PATH"] = str(TMPDIR / "treefrog.db")
os.environ["CF_API_TOKEN"] = "fake"
os.environ["CF_ACCOUNT_ID"] = "fake"
os.environ["CF_KV_NAMESPACE_ID"] = "fake"
os.environ["ADMIN_TOKEN"] = "test-token"

import importlib
import engine.config
importlib.reload(engine.config)
import engine.db
importlib.reload(engine.db)
import engine.scheduler
importlib.reload(engine.scheduler)
import engine.__main__
importlib.reload(engine.__main__)


def test_subcommands_known():
    """All advertised subcommands are accepted by the parser.

    Note: `seed` requires `--m3u`, so we pass a placeholder. The other
    subcommands take no required args.
    """
    from engine.__main__ import _build_parser
    p = _build_parser()
    cases = [
        (["serve"], "serve"),
        (["seed", "--m3u", "https://example.com/x.m3u"], "seed"),
        (["seed", "--m3u", "https://example.com/x.m3u", "--disable"], "seed"),
        (["check-once"], "check-once"),
        (["publish"], "publish"),
        (["migrate"], "migrate"),
        (["epg-import", "--url", "https://example.com/x.xml"], "epg-import"),
        (["reset-uptime"], "reset-uptime"),
        (["reset-uptime", "--hours", "24", "--no-recompute"], "reset-uptime"),
        (["prune"], "prune"),
        (["prune", "--dry-run"], "prune"),
        (["stats"], "stats"),
    ]
    for argv, expected in cases:
        ns = p.parse_args(argv)
        assert ns.cmd == expected, f"subcommand {expected!r} parsed as {ns.cmd!r}"


def test_serve_handler_is_coroutine():
    """The serve handler must be awaitable from an async context.

    Before the fix, _cmd_serve called the sync scheduler.main() which
    then called asyncio.run() — illegal from inside an already-running
    loop. The handler is now _cmd_serve which awaits _run_forever
    directly. This test pins that contract.
    """
    import inspect
    from engine.__main__ import _cmd_serve
    assert inspect.iscoroutinefunction(_cmd_serve), (
        "_cmd_serve must be an async coroutine function so the async "
        "dispatch in main() can await it without re-entering the loop"
    )


def test_serve_can_be_entered():
    """Verify the dispatch into _cmd_serve actually works without
    crashing on the asyncio.run() double-call bug.

    We don't let it run to completion (the scheduler loops forever);
    we just create the task and immediately cancel it. If the bug
    were back, this would raise RuntimeError before we even get to
    the cancel.
    """
    from engine.__main__ import _build_parser, _cmd_serve

    async def _probe():
        task = asyncio.create_task(_cmd_serve(_build_parser().parse_args(["serve"])))
        # Give it a moment to start the inner loop
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_probe())  # should not raise RuntimeError


def test_env_file_fallback_on_missing_file(caplog):
    """Regression: a missing *_FILE path used to crash the engine on
    startup. It should now log a warning and fall through to the
    inline value (or empty, if not set).
    """
    import logging
    os.environ["MISSING_FILE"] = "/no/such/path/anywhere"
    os.environ["MISSING"] = "inline-value"
    try:
        with caplog.at_level(logging.WARNING, logger="treefrog.config"):
            got = engine.config._env("MISSING")
        assert got == "inline-value", f"expected fallback to inline, got {got!r}"
        # Confirm the warning was actually logged so the operator sees
        # the misconfiguration rather than a silent degradation.
        assert any("MISSING_FILE" in r.message for r in caplog.records), (
            "expected a warning about MISSING_FILE"
        )
    finally:
        os.environ.pop("MISSING_FILE", None)
        os.environ.pop("MISSING", None)


def test_env_file_existing_file_still_works():
    """Positive case: when the *_FILE path actually points at a real
    file, its content is returned (not the inline value).
    """
    f = Path(tempfile.mkdtemp()) / "secret.txt"
    f.write_text("file-content\n")
    os.environ["PRESENT_FILE"] = str(f)
    os.environ["PRESENT"] = "should-be-ignored"
    try:
        got = engine.config._env("PRESENT")
        assert got == "file-content"
    finally:
        os.environ.pop("PRESENT_FILE", None)
        os.environ.pop("PRESENT", None)


def test_seed_publishes_public_assets_to_kv(monkeypatch, caplog):
    """Regression: `seed` used to write to disk only. The public Worker
    reads from KV, so the playlist/catalog stayed empty until the next
    scheduled health cycle (up to 30 min later). Seed must now also
    call publish_public_assets(force=True) and publish_redirects(force=True)
    so both the public catalog/playlist AND the per-channel /s/<token>
    redirects land in KV immediately.
    """
    import logging
    from engine import __main__ as cli

    calls = {"publish_public": 0, "publish_redirects": 0, "playlist": 0, "catalog": 0}

    async def _fake_import_m3u(*_a, **_kw):
        return {"channels_new": 1, "streams_new": 1, "duplicates": 0, "total": 1, "errors": 0}

    async def _fake_write_playlist():
        calls["playlist"] += 1
        return "fake-playlist"

    async def _fake_write_catalog():
        calls["catalog"] += 1
        return "fake-catalog"

    async def _fake_publish_public_assets(*, force=False):
        calls["publish_public"] += 1
        assert force is True, "publish must be forced on seed/check-once"
        return {"written": 2, "unchanged": 0, "errors": 0}

    async def _fake_publish_redirects(*, force=False):
        calls["publish_redirects"] += 1
        assert force is True, "publish_redirects must be forced on seed/check-once"
        return {"written": 1, "deleted": 0, "unchanged": 0, "errors": 0, "total": 1}

    monkeypatch.setattr(cli, "import_m3u", _fake_import_m3u)
    monkeypatch.setattr(cli, "write_playlist", _fake_write_playlist)
    monkeypatch.setattr(cli, "write_catalog", _fake_write_catalog)
    monkeypatch.setattr(cli, "publish_public_assets", _fake_publish_public_assets)
    monkeypatch.setattr(cli, "publish_redirects", _fake_publish_redirects)

    with caplog.at_level(logging.INFO, logger="treefrog"):
        rc = asyncio.run(cli._cmd_seed(
            cli._build_parser().parse_args(["seed", "--m3u", "https://example.com/x.m3u"])
        ))

    assert rc == 0
    assert calls == {"publish_public": 1, "publish_redirects": 1, "playlist": 1, "catalog": 1}, (
        f"seed must call write_playlist, write_catalog, publish_public_assets, "
        f"and publish_redirects exactly once each; got {calls}"
    )


def test_admin_import_publishes_public_assets_to_kv(monkeypatch):
    """Regression: the admin /api/admin/import handler used to write
    to disk only. Now it must also push the playlist+catalog AND the
    per-channel redirect tokens to KV so the public site reflects the
    import without waiting for the next health cycle.
    """
    from aiohttp.test_utils import make_mocked_request
    from engine.admin import server

    calls = {"publish_public": 0, "publish_redirects": 0, "playlist": 0, "catalog": 0}

    async def _fake_import_m3u(*_a, **_kw):
        return {"channels_new": 1, "streams_new": 1, "duplicates": 0, "total": 1, "errors": 0}

    async def _fake_write_playlist():
        calls["playlist"] += 1
        return "ok"

    async def _fake_write_catalog():
        calls["catalog"] += 1
        return "ok"

    async def _fake_publish_public_assets(*, force=False):
        calls["publish_public"] += 1
        assert force is True
        return {"written": 2, "unchanged": 0, "errors": 0}

    async def _fake_publish_redirects(*, force=False):
        calls["publish_redirects"] += 1
        assert force is True
        return {"written": 1, "deleted": 0, "unchanged": 0, "errors": 0, "total": 1}

    monkeypatch.setattr(server, "import_m3u", _fake_import_m3u)
    monkeypatch.setattr(server, "write_playlist", _fake_write_playlist)
    monkeypatch.setattr(server, "write_catalog", _fake_write_catalog)
    monkeypatch.setattr(server, "publish_public_assets", _fake_publish_public_assets)
    monkeypatch.setattr(server, "publish_redirects", _fake_publish_redirects)

    # Build a request whose .json() returns our body. We don't need
    # real body parsing — the handler only reads two fields (url, label).
    req = make_mocked_request("POST", "/api/admin/import")

    async def _fake_json():
        return {"url": "https://example.com/x.m3u", "label": "t"}

    monkeypatch.setattr(req, "json", _fake_json)

    resp = asyncio.run(server.handle_admin_import(req))
    assert resp.status == 200, f"expected 200, got {resp.status}"
    assert calls == {"publish_public": 1, "publish_redirects": 1, "playlist": 1, "catalog": 1}, (
        f"handle_admin_import must call write_playlist, write_catalog, "
        f"publish_public_assets, and publish_redirects exactly once each; "
        f"got {calls}"
    )


def test_root_handler_returns_friendly_landing():
    """GET / should return a JSON pointer to the public site + admin
    UI instead of aiohttp's default 404. The engine is API-only; the
    landing response makes that clear so an operator typing the
    Tailscale IP into a browser gets something useful."""
    import json as _json
    from aiohttp.test_utils import make_mocked_request
    from engine.admin import server

    req = make_mocked_request("GET", "/")
    resp = asyncio.run(server.handle_root(req))
    assert resp.status == 200
    body = _json.loads(resp.body)
    assert body["service"] == "treefrog-engine"
    # `endpoints` lists include the HTTP verb prefix in the admin
    # array (e.g. "GET  /api/admin/stats"); assert on substring so
    # the test doesn't break if we reformat the list later.
    public_joined = " ".join(body["endpoints"]["public"])
    admin_joined = " ".join(body["endpoints"]["admin"])
    assert "/api/channels.json" in public_joined
    assert "/api/admin/stats" in admin_joined


def test_admin_ui_handler_injects_token(monkeypatch, tmp_path):
    """GET /admin (or /admin/) should serve the bound-in admin
    index.html with a <meta name="admin-token"> tag containing the
    engine's configured ADMIN_TOKEN. The admin UI's JavaScript reads
    that meta tag and sends it as `Authorization: Bearer ...` on
    every /api/admin/* call.

    We point the handler at a tmp directory with a synthetic
    index.html so the test doesn't depend on the real static asset
    being mounted.
    """
    import json as _json
    from aiohttp.test_utils import make_mocked_request
    from engine.admin import server

    fake_index = tmp_path / "index.html"
    fake_index.write_text(
        "<!doctype html><html><head><title>x</title></head><body></body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_ADMIN_STATIC_DIR", tmp_path)

    req = make_mocked_request("GET", "/admin/")
    resp = asyncio.run(server.handle_admin_ui(req))
    assert resp.status == 200, f"expected 200, got {resp.status}"
    body = resp.body.decode("utf-8")
    assert "<meta name=\"admin-token\"" in body
    # The token in the meta tag must match CONFIG.admin_token
    import re
    m = re.search(r'<meta name="admin-token" content="([^"]*)"', body)
    assert m, "admin-token meta tag missing"
    from engine.config import CONFIG
    assert m.group(1) == CONFIG.admin_token
    # And Cache-Control must be no-store so a token change takes effect
    assert resp.headers.get("Cache-Control") == "no-store"


def test_admin_ui_handler_503_when_assets_unmounted(monkeypatch, tmp_path):
    """If the static assets aren't bound in (e.g. dev environment
    without docker-compose), the handler returns 503 with a clear
    message rather than aiohttp's default 404."""
    from aiohttp.test_utils import make_mocked_request
    from engine.admin import server

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setattr(server, "_ADMIN_STATIC_DIR", empty_dir)

    req = make_mocked_request("GET", "/admin/")
    resp = asyncio.run(server.handle_admin_ui(req))
    assert resp.status == 503
    assert "not mounted" in resp.text.lower()


@pytest.mark.asyncio
async def test_reset_uptime_drops_recent_health_logs(monkeypatch, tmp_path):
    """`reset-uptime --hours 24` should drop the most recent 24h of
    health_logs. We seed two rows (one old, one recent), one stream,
    and one channel. After the reset only the old row should remain.
    """
    import sqlite3
    import aiosqlite
    from engine import __main__ as cli

    db_file = tmp_path / "reset.db"
    _seed_minimal_db(db_file)

    # Swap out engine.db.open_db (and the symbol re-exported into
    # engine.__main__) with a function that opens *our* file. We
    # also have to point the async-context-manager-style close() —
    # the real open_db returns a connection whose .close() is async;
    # we mirror that.
    async def _open_my_db():
        conn = await aiosqlite.connect(str(db_file))
        conn.row_factory = aiosqlite.Row
        return conn
    monkeypatch.setattr(cli, "open_db", _open_my_db)
    # And stub the catalog republish so we don't need a real public dir.
    async def _noop():
        return None
    monkeypatch.setattr(cli, "write_catalog", _noop)

    rc = await cli._cmd_reset_uptime(
        cli._build_parser().parse_args(["reset-uptime", "--hours", "24"])
    )
    assert rc == 0

    # Verify the recent row is gone and the old one is kept. The
    # surviving row's timestamp must be more than 24h old (the
    # `--hours 24` cutoff). The seeded "30 days ago" row lives
    # outside the window so it survives.
    conn = sqlite3.connect(str(db_file))
    rows = conn.execute("SELECT checked_at FROM health_logs").fetchall()
    conn.close()
    assert len(rows) == 1, f"expected 1 health_log row, got {len(rows)}"
    # The old row's `checked_at` is a SQLite-formatted timestamp that
    # is well before `now - 24h`. We just need to know we didn't
    # accidentally keep the recent (1h ago) row.
    from datetime import datetime, timedelta
    survived = datetime.strptime(rows[0][0], "%Y-%m-%d %H:%M:%S")
    age = datetime.now() - survived
    assert age > timedelta(hours=24), (
        f"surviving row is too recent ({age}); the cutoff isn't being applied"
    )


@pytest.mark.asyncio
async def test_reset_uptime_no_recompute_skips_republish(monkeypatch, tmp_path):
    """`--no-recompute` must skip the catalog republish — the operator
    just wants to drop history and let the next health cycle fill it
    back in. The DELETE always runs (covered by the previous test);
    this one is scoped tight to the no-recompute branch."""
    from engine import __main__ as cli
    import aiosqlite

    db_file = tmp_path / "norecomp.db"
    _seed_minimal_db(db_file)

    async def _open_my_db():
        conn = await aiosqlite.connect(str(db_file))
        conn.row_factory = aiosqlite.Row
        return conn
    monkeypatch.setattr(cli, "open_db", _open_my_db)

    write_catalog_called = False
    async def _spy_write_catalog():
        nonlocal write_catalog_called
        write_catalog_called = True
    monkeypatch.setattr(cli, "write_catalog", _spy_write_catalog)

    rc = await cli._cmd_reset_uptime(
        cli._build_parser().parse_args(["reset-uptime", "--no-recompute"])
    )
    assert rc == 0
    assert not write_catalog_called, "no-recompute must skip write_catalog"


@pytest.mark.asyncio
async def test_cmd_prune_dry_run_reports_no_changes(monkeypatch):
    """`python -m engine prune --dry-run` calls prune_dead_playlists
    with dry_run=True and does NOT republish. The republish is the
    side-effect we want to gate on a real prune.

    We stub the pruner entirely so the test exercises the CLI
    plumbing (dispatch, dry_run flag, conditional republish) without
    needing a real DB with source_label rows.
    """
    from engine import __main__ as cli

    captured = {"dry_run": None, "called": 0}

    async def _fake_prune(db, *, dry_run=False):
        captured["called"] += 1
        captured["dry_run"] = dry_run
        return {
            "dry_run": dry_run,
            "scanned_labels": 3,
            "dead_labels": 0,
            "pruned": [],
        }

    monkeypatch.setattr(cli, "prune_dead_playlists", _fake_prune)

    class _FakeDB:
        """Stand-in for an aiosqlite connection. Has an async close()
        so the handler's `await db.close()` in the finally block
        doesn't blow up."""
        async def close(self):
            pass
    async def _fake_open():
        return _FakeDB()
    monkeypatch.setattr(cli, "open_db", _fake_open)

    calls = {"publish_public": 0, "publish_redirects": 0, "playlist": 0, "catalog": 0}

    async def _spy(*_a, **_kw):
        calls["publish_public"] += 1
    async def _spy2(*_a, **_kw):
        calls["publish_redirects"] += 1
    async def _spy3():
        calls["playlist"] += 1
    async def _spy4():
        calls["catalog"] += 1

    monkeypatch.setattr(cli, "write_playlist", _spy3)
    monkeypatch.setattr(cli, "write_catalog", _spy4)
    monkeypatch.setattr(cli, "publish_public_assets", _spy)
    monkeypatch.setattr(cli, "publish_redirects", _spy2)

    # dry_run with no deletions → no republish.
    rc = await cli._cmd_prune(
        cli._build_parser().parse_args(["prune", "--dry-run"])
    )
    assert rc == 0
    assert captured["called"] == 1
    assert captured["dry_run"] is True
    assert calls == {"publish_public": 0, "publish_redirects": 0, "playlist": 0, "catalog": 0}, (
        f"dry-run prune must not republish; got {calls}"
    )


@pytest.mark.asyncio
async def test_cmd_prune_republishes_on_real_deletion(monkeypatch):
    """When the pruner actually deletes something, _cmd_prune must
    republish the catalog + KV so the public site sees the cleanup
    immediately rather than waiting for the next tick."""
    from engine import __main__ as cli

    async def _fake_prune(db, *, dry_run=False):
        return {
            "dry_run": False,
            "scanned_labels": 5,
            "dead_labels": 2,
            "pruned": [
                {"source_label": "a", "streams_deleted": 3, "channels_deleted": 2},
                {"source_label": "b", "streams_deleted": 1, "channels_deleted": 0},
            ],
        }

    monkeypatch.setattr(cli, "prune_dead_playlists", _fake_prune)

    class _FakeDB:
        """Stand-in for an aiosqlite connection. Has an async close()
        so the handler's `await db.close()` in the finally block
        doesn't blow up."""
        async def close(self):
            pass
    async def _fake_open():
        return _FakeDB()
    monkeypatch.setattr(cli, "open_db", _fake_open)

    calls = {"publish_public": 0, "publish_redirects": 0, "playlist": 0, "catalog": 0}

    async def _spy(*_a, **_kw):
        calls["publish_public"] += 1
    async def _spy2(*_a, **_kw):
        calls["publish_redirects"] += 1
    async def _spy3():
        calls["playlist"] += 1
    async def _spy4():
        calls["catalog"] += 1

    monkeypatch.setattr(cli, "write_playlist", _spy3)
    monkeypatch.setattr(cli, "write_catalog", _spy4)
    monkeypatch.setattr(cli, "publish_public_assets", _spy)
    monkeypatch.setattr(cli, "publish_redirects", _spy2)

    rc = await cli._cmd_prune(cli._build_parser().parse_args(["prune"]))
    assert rc == 0
    assert calls == {"publish_public": 1, "publish_redirects": 1, "playlist": 1, "catalog": 1}, (
        f"real prune must republish all four; got {calls}"
    )


def test_seed_disable_flag_passes_disabled_to_importer(monkeypatch, caplog):
    """`python -m engine seed --m3u URL --label L --disable` must call
    `import_m3u(..., disabled=True)` so the imported streams are
    inserted as status='disabled' (warm backup). This is what lets
    the distrotv-proxy container be imported as a backup without
    serving traffic or getting pruned.
    """
    import logging
    from engine import __main__ as cli

    captured = {"disabled": None, "called": 0}

    async def _fake_import_m3u(*_a, **kw):
        captured["called"] += 1
        captured["disabled"] = kw.get("disabled")
        captured["source_label"] = kw.get("source_label")
        return {
            "channels_new": 1, "streams_new": 1, "duplicates": 0,
            "total": 1, "errors": 0, "disabled": kw.get("disabled", False),
        }

    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr(cli, "import_m3u", _fake_import_m3u)
    monkeypatch.setattr(cli, "write_playlist", _noop)
    monkeypatch.setattr(cli, "write_catalog", _noop)
    monkeypatch.setattr(cli, "publish_public_assets", _noop)
    monkeypatch.setattr(cli, "publish_redirects", _noop)

    with caplog.at_level(logging.INFO, logger="treefrog"):
        rc = asyncio.run(cli._cmd_seed(
            cli._build_parser().parse_args(
                ["seed", "--m3u", "https://example.com/x.m3u",
                 "--label", "Backup", "--disable"]
            )
        ))

    assert rc == 0
    assert captured["called"] == 1
    assert captured["disabled"] is True, (
        "seed --disable must pass disabled=True to import_m3u so streams "
        "are inserted as status='disabled' (not status='unknown')"
    )
    assert captured["source_label"] == "Backup"


def test_seed_default_does_not_disable(monkeypatch):
    """Sanity: `seed` without `--disable` must pass disabled=False to
    the importer. Otherwise every import would be a backup and
    nothing would ever go live."""
    from engine import __main__ as cli

    captured = {"disabled": None, "called": 0}

    async def _fake_import_m3u(*_a, **kw):
        captured["called"] += 1
        captured["disabled"] = kw.get("disabled")
        return {
            "channels_new": 1, "streams_new": 1, "duplicates": 0,
            "total": 1, "errors": 0, "disabled": kw.get("disabled", False),
        }

    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr(cli, "import_m3u", _fake_import_m3u)
    monkeypatch.setattr(cli, "write_playlist", _noop)
    monkeypatch.setattr(cli, "write_catalog", _noop)
    monkeypatch.setattr(cli, "publish_public_assets", _noop)
    monkeypatch.setattr(cli, "publish_redirects", _noop)

    rc = asyncio.run(cli._cmd_seed(
        cli._build_parser().parse_args(
            ["seed", "--m3u", "https://example.com/x.m3u", "--label", "Active"]
        )
    ))

    assert rc == 0
    assert captured["called"] == 1
    assert captured["disabled"] is False, (
        "seed without --disable must NOT set disabled=True — that would "
        "make every import a backup and no streams would ever go live"
    )


@pytest.mark.asyncio
async def test_admin_prune_calls_pruner(monkeypatch):
    """POST /api/admin/prune[?dry_run=1] must call the pruner with
    the right dry_run flag, and the dry_run variant must NOT trigger
    a republish. Uses a mocked request + the same aiohttp pattern as
    the other admin tests."""
    from aiohttp.test_utils import make_mocked_request
    from engine.admin import server

    captured = {"dry_run": None, "called": 0}

    async def _fake_prune(db, *, dry_run=False):
        captured["called"] += 1
        captured["dry_run"] = dry_run
        return {
            "dry_run": dry_run,
            "scanned_labels": 0,
            "dead_labels": 0,
            "pruned": [],
        }

    monkeypatch.setattr(server, "prune_dead_playlists", _fake_prune)

    class _FakeDB:
        async def close(self):
            pass
    async def _fake_open():
        return _FakeDB()
    monkeypatch.setattr(server, "open_db", _fake_open)

    republish_calls = {"playlist": 0, "catalog": 0, "redirects": 0, "public": 0}
    async def _spy_playlist():
        republish_calls["playlist"] += 1
    async def _spy_catalog():
        republish_calls["catalog"] += 1
    async def _spy_redirects(*_a, **_kw):
        republish_calls["redirects"] += 1
    async def _spy_public(*_a, **_kw):
        republish_calls["public"] += 1
    monkeypatch.setattr(server, "write_playlist", _spy_playlist)
    monkeypatch.setattr(server, "write_catalog", _spy_catalog)
    monkeypatch.setattr(server, "publish_redirects", _spy_redirects)
    monkeypatch.setattr(server, "publish_public_assets", _spy_public)

    # 1. dry_run=1 → no republish
    req = make_mocked_request("POST", "/api/admin/prune?dry_run=1")
    resp = await server.handle_admin_prune(req)
    assert resp.status == 200
    assert captured["called"] == 1
    assert captured["dry_run"] is True
    assert republish_calls == {"playlist": 0, "catalog": 0, "redirects": 0, "public": 0}

    # 2. no query param → dry_run=False; pruner returned dead_labels=0
    #    so the republish branch is skipped (handler only republishes
    #    if anything was actually deleted).
    captured["called"] = 0
    req = make_mocked_request("POST", "/api/admin/prune")
    resp = await server.handle_admin_prune(req)
    assert resp.status == 200
    assert captured["called"] == 1
    assert captured["dry_run"] is False
    assert republish_calls == {"playlist": 0, "catalog": 0, "redirects": 0, "public": 0}


@pytest.mark.asyncio
async def test_admin_prune_republishes_on_real_deletion(monkeypatch):
    """When the pruner actually deletes something (dead_labels > 0),
    the handler must republish the catalog + KV so the public site
    sees the cleanup immediately."""
    from aiohttp.test_utils import make_mocked_request
    from engine.admin import server

    async def _fake_prune(db, *, dry_run=False):
        return {
            "dry_run": False,
            "scanned_labels": 5,
            "dead_labels": 1,
            "pruned": [{"source_label": "dead-m3u", "streams_deleted": 3, "channels_deleted": 2}],
        }

    monkeypatch.setattr(server, "prune_dead_playlists", _fake_prune)

    class _FakeDB:
        async def close(self):
            pass
    async def _fake_open():
        return _FakeDB()
    monkeypatch.setattr(server, "open_db", _fake_open)

    republish_calls = {"playlist": 0, "catalog": 0, "redirects": 0, "public": 0}
    async def _spy_playlist():
        republish_calls["playlist"] += 1
    async def _spy_catalog():
        republish_calls["catalog"] += 1
    async def _spy_redirects(*_a, **_kw):
        republish_calls["redirects"] += 1
    async def _spy_public(*_a, **_kw):
        republish_calls["public"] += 1
    monkeypatch.setattr(server, "write_playlist", _spy_playlist)
    monkeypatch.setattr(server, "write_catalog", _spy_catalog)
    monkeypatch.setattr(server, "publish_redirects", _spy_redirects)
    monkeypatch.setattr(server, "publish_public_assets", _spy_public)

    req = make_mocked_request("POST", "/api/admin/prune")
    resp = await server.handle_admin_prune(req)
    assert resp.status == 200
    # Real deletion → all four republish steps fired.
    assert republish_calls == {"playlist": 1, "catalog": 1, "redirects": 1, "public": 1}


def _seed_minimal_db(path: Path) -> None:
    """Create the three tables the reset handler touches and seed
    one channel, one stream, two health_logs (one old, one recent).
    The 'recent' row is the one reset-uptime should drop."""
    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE streams (
            id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY,
            display_name TEXT,
            availability_pct REAL NOT NULL DEFAULT 100.0,
            last_checked_at TEXT,
            updated_at TEXT,
            status TEXT NOT NULL DEFAULT 'online'
        );
        CREATE TABLE health_logs (
            id INTEGER PRIMARY KEY,
            stream_id INTEGER NOT NULL,
            checked_at TEXT NOT NULL,
            ok INTEGER NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO channels (id, display_name, availability_pct, status) "
        "VALUES (1, 'Test', 0.0, 'online')"
    )
    conn.execute(
        "INSERT INTO streams (id, channel_id, url, status) "
        "VALUES (1, 1, 'http://example/stream', 'online')"
    )
    conn.execute(
        "INSERT INTO health_logs (stream_id, checked_at, ok) "
        "VALUES (1, datetime('now', '-30 days'), 1)"  # old → keep
    )
    conn.execute(
        "INSERT INTO health_logs (stream_id, checked_at, ok) "
        "VALUES (1, datetime('now', '-1 hour'), 0)"   # recent → drop
    )
    conn.commit()
    conn.close()
