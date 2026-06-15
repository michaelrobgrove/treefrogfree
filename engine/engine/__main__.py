"""CLI entrypoint.

Usage:
    python -m engine serve              # default — run the scheduler loop
    python -m engine seed --m3u <url>   # one-shot M3U import
    python -m engine check-once         # run a single health cycle
    python -m engine publish            # re-render playlist + catalog only
    python -m engine migrate            # apply DB migrations and exit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .config import CONFIG
from .db import open_db, run_migrations
from .health import run_health_cycle
from .importers.importer import import_m3u
from .publisher.json_catalog import write_catalog
from .publisher.playlist import write_playlist

log = logging.getLogger("treefrog")


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, CONFIG.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="engine",
        description="Tree Frog Streams — IPTV channel registry, health monitor, and playlist publisher.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="Run the long-lived scheduler (default)")

    seed = sub.add_parser("seed", help="Import an M3U from URL or file path")
    seed.add_argument("--m3u", required=True, help="URL or local file path")
    seed.add_argument("--label", default=None, help="Optional source label")

    sub.add_parser("check-once", help="Run a single health-check cycle")
    sub.add_parser("publish", help="Re-render playlist + catalog JSON only")
    sub.add_parser("migrate", help="Apply pending DB migrations and exit")

    stats = sub.add_parser("stats", help="Print summary stats and exit")
    stats.add_argument("--json", action="store_true", help="Output JSON instead of text")

    return p


async def _cmd_serve(_args: argparse.Namespace) -> int:
    # Defer to scheduler.main so signal handling is consistent.
    from .scheduler import main as _scheduler_main

    _scheduler_main()
    return 0


async def _cmd_seed(args: argparse.Namespace) -> int:
    summary = await import_m3u(args.m3u, source_label=args.label)
    print(json.dumps(summary, indent=2))
    return 0


async def _cmd_check_once(_args: argparse.Namespace) -> int:
    summary = await run_health_cycle()
    print(json.dumps(summary, indent=2))
    return 0


async def _cmd_publish(_args: argparse.Namespace) -> int:
    p1 = await write_playlist()
    p2 = await write_catalog()
    print(f"playlist: {p1}")
    print(f"catalog:  {p2}")
    return 0


async def _cmd_migrate(_args: argparse.Namespace) -> int:
    db = await open_db()
    try:
        await run_migrations(db)
    finally:
        await db.close()
    print(f"migrations applied; db at {CONFIG.db_path}")
    return 0


async def _cmd_stats(args: argparse.Namespace) -> int:
    db = await open_db()
    try:
        async with db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM channels WHERE status='online')  AS online_channels,
                (SELECT COUNT(*) FROM channels WHERE status='offline') AS offline_channels,
                (SELECT COUNT(*) FROM streams  WHERE status='online')  AS online_streams,
                (SELECT COUNT(*) FROM streams  WHERE status='offline') AS offline_streams,
                (SELECT COUNT(*) FROM streams  WHERE status='disabled') AS disabled_streams,
                (SELECT COUNT(*) FROM imports) AS import_count,
                (SELECT MAX(last_checked_at) FROM channels) AS last_check
            """
        ) as cur:
            row = await cur.fetchone()
        if args.json:
            print(json.dumps(dict(row), indent=2))
        else:
            print(f"Online channels:   {row['online_channels']}")
            print(f"Offline channels:  {row['offline_channels']}")
            print(f"Online streams:    {row['online_streams']}")
            print(f"Offline streams:   {row['offline_streams']}")
            print(f"Disabled streams:  {row['disabled_streams']}")
            print(f"Imports run:       {row['import_count']}")
            print(f"Last health check: {row['last_check']}")
        return 0
    finally:
        await db.close()


_HANDLERS = {
    "serve": _cmd_serve,
    "seed": _cmd_seed,
    "check-once": _cmd_check_once,
    "publish": _cmd_publish,
    "migrate": _cmd_migrate,
    "stats": _cmd_stats,
}


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = _build_parser().parse_args(argv)
    handler = _HANDLERS[args.cmd]
    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        log.exception("Command failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
