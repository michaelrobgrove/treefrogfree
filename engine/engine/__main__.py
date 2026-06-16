"""CLI entrypoint.

Usage:
    python -m engine serve              # default — run the scheduler loop
    python -m engine seed --m3u <url>   # one-shot M3U import
    python -m engine check-once         # run a single health cycle
    python -m engine publish            # re-render playlist + catalog only
    python -m engine migrate            # apply DB migrations and exit
    python -m engine epg-import --url <xmltv-url>  # one-shot XMLTV import
    python -m engine prune [--dry-run]  # drop dead playlists (0 online streams)
    python -m engine reset-uptime       # clear stale availability_pct values
    python -m engine stats              # print summary stats
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .admin.epg import import_epg_url
from .config import CONFIG
from .db import open_db, run_migrations
from .health import run_health_cycle
from .importers.importer import import_m3u
from .pruner import prune_dead_playlists
from .publisher.json_catalog import write_catalog
from .publisher.kv import publish_public_assets, publish_redirects
from .publisher.playlist import write_playlist
from .publisher.streams_kv import publish_stream_lists

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

    epg_import = sub.add_parser(
        "epg-import",
        help="One-shot XMLTV import from a URL (gzipped XMLTV is auto-detected)",
    )
    epg_import.add_argument("--url", required=True, help="URL to fetch the XMLTV from")
    epg_import.add_argument(
        "--publish-nownext",
        action="store_true",
        help="After import, also publish epg:nownext:<tvg_id> JSON to KV",
    )

    stats = sub.add_parser("stats", help="Print summary stats and exit")
    stats.add_argument("--json", action="store_true", help="Output JSON instead of text")

    reset = sub.add_parser(
        "reset-uptime",
        help=(
            "Reset stale availability_pct values. Drops the most recent "
            "N hours of health_logs and recomputes the 7d rolling "
            "average so the public site shows a clean slate."
        ),
    )
    reset.add_argument(
        "--hours",
        type=int,
        default=168,  # 7 days — the full rolling window
        help="Drop health_logs newer than this many hours (default: 168 = 7d)",
    )
    reset.add_argument(
        "--no-recompute",
        action="store_true",
        help="Skip the post-reset availability_pct recompute (let the next health cycle do it)",
    )

    prune = sub.add_parser(
        "prune",
        help=(
            "Drop any source_label whose streams are ALL offline, plus "
            "the channels that would become orphaned. Idempotent and "
            "safe to run any time. The scheduler also runs this at the "
            "end of every health cycle; the subcommand is for an "
            "operator-driven sweep without waiting for the next tick."
        ),
    )
    prune.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be deleted without touching the DB",
    )

    return p


async def _cmd_serve(_args: argparse.Namespace) -> int:
    # Defer to scheduler._run_forever so signal handling and shutdown
    # are consistent with the scheduler's own entry point. The async
    # dispatch in main() already wraps us in an event loop, so we just
    # await the coroutine rather than calling asyncio.run() again.
    from .scheduler import _run_forever

    await _run_forever()
    return 0


async def _cmd_seed(args: argparse.Namespace) -> int:
    summary = await import_m3u(args.m3u, source_label=args.label)
    # Republish to disk and KV so the public site reflects the new
    # import immediately, not 30 minutes later at the next health cycle.
    # write_playlist mints redirect tokens for newly-online channels;
    # publish_redirects then pushes those tokens to KV so /s/<token>
    # 302s resolve; publish_public_assets pushes the catalog/playlist
    # snapshot.
    await write_playlist()
    await write_catalog()
    redir = await publish_redirects(force=True)
    pub = await publish_public_assets(force=True)
    log.info("seed: KV redirects=%s, public=%s", redir, pub)
    print(json.dumps(summary, indent=2))
    return 0


async def _cmd_check_once(_args: argparse.Namespace) -> int:
    summary = await run_health_cycle()
    print(json.dumps(summary, indent=2))
    return 0


async def _cmd_publish(_args: argparse.Namespace) -> int:
    p1 = await write_playlist()
    p2 = await write_catalog()
    # write_playlist mints redirect tokens for any newly-online channels;
    # push them so /s/<token> lookups work immediately.
    redir = await publish_redirects(force=True)
    pub = await publish_public_assets(force=True)
    # Also push the per-channel stream lists the HLS player needs.
    # Best-effort: if the module isn't importable yet (mid-deploy),
    # the player just sees 404s on /api/streams/<token> and falls back
    # to the existing /s/<token> 302 redirect.
    try:
        sl = await publish_stream_lists(force=True)
        print(f"kv stream lists: {sl}")
    except Exception as e:
        print(f"kv stream lists: SKIPPED ({type(e).__name__}: {e})")
    try:
        from .publisher.epg_kv import publish_nownext
        nn = await publish_nownext(force=True)
        print(f"kv epg now/next: {nn}")
    except Exception as e:
        print(f"kv epg now/next: SKIPPED ({type(e).__name__}: {e})")
    print(f"playlist: {p1}")
    print(f"catalog:  {p2}")
    print(f"kv redirects: {redir}")
    print(f"kv public:    {pub}")
    return 0


async def _cmd_migrate(_args: argparse.Namespace) -> int:
    db = await open_db()
    try:
        await run_migrations(db)
    finally:
        await db.close()
    print(f"migrations applied; db at {CONFIG.db_path}")
    return 0


async def _cmd_epg_import(args: argparse.Namespace) -> int:
    """One-shot XMLTV import. Useful when the admin UI isn't reachable
    (e.g. before the bind-mount is in place) or for scripting.

    Examples:
        python -m engine epg-import --url https://example.com/guide.xml.gz
        python -m engine epg-import --url https://example.com/guide.xml.gz --publish-nownext

    With --publish-nownext, also pushes per-tvg-id "on now" / "up next"
    JSON blobs to KV so the HLS player's EPG panel lights up immediately.
    """
    print(f"epg-import: fetching {args.url} ...", flush=True)
    try:
        summary = await import_epg_url(args.url)
    except Exception as e:
        log.exception("EPG import failed")
        print(f"epg-import: FAILED: {type(e).__name__}: {e}")
        return 1
    print(json.dumps(summary, indent=2))
    if args.publish_nownext:
        try:
            from .publisher.epg_kv import publish_nownext
            nn = await publish_nownext(force=True)
            print(json.dumps(nn, indent=2))
        except Exception as e:
            log.exception("publish_nownext failed")
            print(f"publish_nownext: FAILED: {type(e).__name__}: {e}")
            # Don't return 1 — the import itself succeeded; KV is best-effort.
    return 0


async def _cmd_reset_uptime(args: argparse.Namespace) -> int:
    """Clear stale availability_pct values.

    The 7d rolling average is contaminated by the pre-VLC-UA era of
    failed probes (HEAD 403s on providers that don't allow HEAD, etc.),
    which dragged every channel's score down. This subcommand drops
    the last N hours of health_logs (default: the full 7d window) and
    re-runs the recompute so the next /api/channels.json shows a
    clean slate.

    Idempotent. Safe to run multiple times. The streams themselves
    are untouched; only the history rolls.
    """
    db = await open_db()
    try:
        async with db.execute(
            "DELETE FROM health_logs WHERE checked_at >= datetime('now', ?)",
            (f"-{args.hours} hours",),
        ) as cur:
            deleted = cur.rowcount or 0
        log.info("reset-uptime: dropped %d health_logs rows (last %dh)",
                 deleted, args.hours)

        if not args.no_recompute:
            # Mirror the rolling-average SQL from health._recompute_channel_status
            # but only the availability_pct column. We don't touch
            # channels.status — that's driven by the streams table, not
            # by the rolling average.
            await db.execute(
                """
                UPDATE channels
                SET availability_pct = COALESCE((
                    SELECT AVG(ok_pct)
                    FROM (
                        SELECT
                            CASE WHEN COUNT(*) = 0 THEN 1.0
                                 ELSE 1.0 * SUM(h.ok) / COUNT(*)
                            END AS ok_pct
                        FROM health_logs h
                        JOIN streams s ON s.id = h.stream_id
                        WHERE s.channel_id = channels.id
                          AND h.checked_at >= datetime('now', '-7 days')
                        GROUP BY s.id
                    )
                ), 1.0) * 100
                """
            )
            await db.execute(
                "UPDATE channels SET updated_at = datetime('now')"
            )
        await db.commit()
    finally:
        await db.close()

    if not args.no_recompute:
        # Republish the catalog so the public site shows the new numbers
        # without waiting for the next 30m cycle.
        try:
            await write_catalog()
            from .publisher.kv import publish_public_assets
            pub = await publish_public_assets(force=True)
            log.info("reset-uptime: republished catalog + KV: %s", pub)
        except Exception as e:
            log.warning("reset-uptime: catalog republish failed: %s", e)

    print(json.dumps({
        "dropped_health_logs": deleted,
        "window_hours": args.hours,
        "recomputed": not args.no_recompute,
    }, indent=2))
    return 0


async def _cmd_prune(args: argparse.Namespace) -> int:
    """Drop dead playlists (source_labels with 0 online streams) and
    any channels that lose their last stream as a result.

    Idempotent: the second call is a no-op. Safe to run any time.
    With `--dry-run`, returns the kill list without mutating the DB.
    The scheduler calls the same function at the end of every tick;
    this CLI is for an on-demand operator sweep.
    """
    db = await open_db()
    try:
        summary = await prune_dead_playlists(db, dry_run=args.dry_run)
    finally:
        await db.close()
    print(json.dumps(summary, indent=2))
    # If we actually deleted anything, republish the public catalog +
    # KV so the changes are visible immediately rather than waiting
    # for the next 30m tick. (In dry-run mode we skip the republish
    # because nothing changed.)
    if not summary["dry_run"] and summary["dead_labels"] > 0:
        try:
            await write_playlist()
            await write_catalog()
            await publish_redirects(force=True)
            await publish_public_assets(force=True)
            log.info(
                "prune: republished after dropping %d label(s) (%d total streams, %d orphan channels)",
                summary["dead_labels"],
                sum(p["streams_deleted"] for p in summary["pruned"]),
                sum(p["channels_deleted"] for p in summary["pruned"]),
            )
        except Exception:
            log.exception("prune: post-prune republish failed; continuing")
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
    "epg-import": _cmd_epg_import,
    "reset-uptime": _cmd_reset_uptime,
    "prune": _cmd_prune,
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
