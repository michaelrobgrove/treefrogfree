"""30-minute scheduler driver. Long-running process.

Default mode: import nothing on startup, then loop forever:
    1. Wait for next tick (drift-free)
    2. Run health cycle
    3. Re-render playlist + catalog JSON
    4. Repeat

Per plan.md §6.2, we use an explicit tick loop instead of `schedule` to
avoid drift and to keep the cycle's CPU profile predictable.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import datetime, timezone

from aiohttp import web

from .config import CONFIG
from .db import open_db, run_migrations
from .health import run_health_cycle
from .publisher.json_catalog import write_catalog
from .publisher.kv import publish_public_assets, publish_redirects
from .publisher.playlist import write_playlist
from .publisher.streams_kv import publish_stream_lists

log = logging.getLogger("treefrog.scheduler")


async def _tick() -> None:
    log.info("=== Health cycle starting ===")
    started = time.monotonic()
    try:
        summary = await run_health_cycle()
        log.info(
            "Health cycle done: checked=%d online=%d offline=%d in %.1fs",
            summary["checked"],
            summary["online"],
            summary["offline"],
            time.monotonic() - started,
        )
    except Exception:
        log.exception("Health cycle failed; continuing")

    try:
        await write_playlist()
    except Exception:
        log.exception("Playlist render failed; continuing")

    try:
        await write_catalog()
    except Exception:
        log.exception("Catalog render failed; continuing")

    # KV is the hot-path store. Push winners so the Worker's /s/* lookups
    # resolve. Skipping this on a cycle is fine — last push still serves.
    try:
        kv_summary = await publish_redirects()
        log.info("KV publish: %s", kv_summary)
    except Exception:
        log.exception("KV publish failed; continuing")

    # Public read path. The Worker serves /api/channels.json and
    # /playlist.m3u straight from KV, so the engine never needs to be
    # publicly reachable.
    try:
        pub_summary = await publish_public_assets()
        log.info("KV public assets: %s", pub_summary)
    except Exception:
        log.exception("KV public assets publish failed; continuing")

    # Per-channel stream lists for the HLS web player. The /s/<token>
    # redirect already points at one URL; the player wants the full
    # ordered list so it can fail over when a source stalls.
    try:
        sl_summary = await publish_stream_lists()
        log.info("KV stream lists: %s", sl_summary)
    except Exception:
        log.exception("KV stream lists publish failed; continuing")

    # EPG refresh (every 6h, see plan.md §10.1). Cheap to do on every cycle
    # since we re-import only if cache is stale — but for now skip the
    # staleness check and just do it on a slow timer.
    if _should_refresh_epg():
        try:
            from .admin.epg import import_epg_default
            await import_epg_default()
        except Exception:
            log.exception("EPG refresh failed; continuing")
        # Push now/next JSON to KV so the player's "On now" block
        # reflects the freshly imported XMLTV on the next read.
        try:
            from .publisher.epg_kv import publish_nownext
            nn = await publish_nownext()
            log.info("KV EPG now/next: %s", nn)
        except Exception:
            log.exception("KV EPG now/next publish failed; continuing")


def _should_refresh_epg() -> bool:
    """True if EPG hasn't been refreshed in the last 6 hours."""
    from pathlib import Path
    from .admin.epg import _meta_path
    p = _meta_path()
    if not p.exists():
        return False  # no EPG sources configured; nothing to do
    age = time.time() - p.stat().st_mtime
    return age > 6 * 3600


async def _run_forever() -> None:
    db = await open_db()
    await run_migrations(db)
    await db.close()

    # Start the admin + public API server in the background. It serves
    # /api/channels.json, /playlist.m3u, /api/epg.xml, and the admin
    # endpoints. See plan.md §11.
    from .admin.server import build_app
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, CONFIG.admin_host, CONFIG.admin_port)
    await site.start()
    log.info("API server listening on http://%s:%d", CONFIG.admin_host, CONFIG.admin_port)

    log.info(
        "Tree Frog scheduler online. cadence=%ds concurrency=%d",
        CONFIG.health_cadence_sec,
        CONFIG.health_concurrency,
    )

    stop = asyncio.Event()

    def _handle_signal() -> None:
        log.info("Shutdown signal received")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows / restricted environments — fall back to KeyboardInterrupt.
            pass

    # Run an initial cycle immediately so the operator sees something happen.
    await _tick()

    last_tick = time.monotonic()
    while not stop.is_set():
        elapsed = time.monotonic() - last_tick
        sleep_for = max(0, CONFIG.health_cadence_sec - elapsed)
        try:
            await asyncio.wait_for(stop.wait(), timeout=sleep_for)
            break  # stop event fired
        except asyncio.TimeoutError:
            pass
        last_tick = time.monotonic()
        await _tick()

    log.info("Scheduler exited cleanly at %s", datetime.now(timezone.utc).isoformat())


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, CONFIG.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_run_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
