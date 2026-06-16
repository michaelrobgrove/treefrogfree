"""M3U import orchestration: stream → consolidate → upsert into DB.

Idempotency: re-importing the same M3U must produce zero new rows.
Strategy: UNIQUE (source_url) on streams, UNIQUE (normalized_name) on
channels. We use INSERT OR IGNORE for both, then count what survived.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

import aiohttp

from ..db import open_db, run_migrations
from ..models import M3UEntry
from .m3u import parse_file, parse_url, looks_like_url
from ..consolidator import canonical_channel_name, normalize

log = logging.getLogger("treefrog.importer")


async def import_m3u(
    source: str,
    *,
    source_label: Optional[str] = None,
    http_session: Optional[aiohttp.ClientSession] = None,
    disabled: bool = False,
) -> dict:
    """Import an M3U from a URL or local file path.

    Args:
        source: URL or local file path.
        source_label: Friendly label (stored in streams.source_label).
        http_session: Optional aiohttp session to share with the caller.
        disabled: If True, the imported streams are inserted with
            status='disabled' instead of 'unknown'. The streams are
            kept in the DB as a warm backup — they don't serve as
            winners in the catalog (only 'online' streams do), and
            the pruner leaves them alone (it only sweeps 'offline'
            labels, not 'disabled' ones). Use this for backup M3U
            sources you want available if the primary dies.

    Returns a summary dict: {channels_new, streams_new, duplicates, total, disabled}.
    """
    if looks_like_url(source):
        iterator_factory = lambda s: parse_url(source, session=s)  # noqa: E731
    else:
        iterator_factory = lambda s: parse_file(source)  # noqa: E731

    own_session = http_session is None
    if own_session:
        http_session = aiohttp.ClientSession()

    db = await open_db()
    await run_migrations(db)
    try:
        # Audit row
        cur = await db.execute(
            "INSERT INTO imports (source_url, source_label) VALUES (?, ?)",
            (source, source_label),
        )
        import_id = cur.lastrowid
        await db.commit()

        summary = {
            "channels_new": 0,
            "streams_new": 0,
            "duplicates": 0,
            "total": 0,
            "errors": 0,
            "disabled": disabled,
        }

        # We stream entries and batch inserts in chunks of 200. SQLite's
        # per-statement overhead dominates at small batch sizes.
        batch: list[M3UEntry] = []
        BATCH = 200

        async for entry in iterator_factory(http_session):
            summary["total"] += 1
            batch.append(entry)
            if len(batch) >= BATCH:
                counts = await _upsert_batch(db, batch, source_label, disabled=disabled)
                summary["channels_new"] += counts["channels_new"]
                summary["streams_new"] += counts["streams_new"]
                summary["duplicates"] += counts["duplicates"]
                batch.clear()

        if batch:
            counts = await _upsert_batch(db, batch, source_label, disabled=disabled)
            summary["channels_new"] += counts["channels_new"]
            summary["streams_new"] += counts["streams_new"]
            summary["duplicates"] += counts["duplicates"]

        # When importing as disabled, the note explicitly says so —
        # the operator can grep the imports table for "imported as
        # disabled backup" to find these rows quickly.
        if disabled:
            note = f"total entries seen: {summary['total']} (imported as disabled backup)"
        else:
            note = f"total entries seen: {summary['total']}"

        await db.execute(
            """
            UPDATE imports
            SET finished_at = datetime('now'),
                channels_new = ?, streams_new = ?, duplicates = ?,
                notes = ?
            WHERE id = ?
            """,
            (
                summary["channels_new"],
                summary["streams_new"],
                summary["duplicates"],
                note,
                import_id,
            ),
        )
        await db.commit()
        log.info(
            "Import done: %d entries, %d new channels, %d new streams, %d duplicates",
            summary["total"],
            summary["channels_new"],
            summary["streams_new"],
            summary["duplicates"],
        )
        return summary
    finally:
        await db.close()
        if own_session:
            await http_session.close()


async def _upsert_batch(
    db, entries: list[M3UEntry], source_label: Optional[str], *, disabled: bool = False
) -> dict:
    """Insert a batch of M3U entries, consolidating by tvg-id → normalized name.

    Returns counts of new channels, new streams, and duplicates.

    When `disabled=True`, newly-inserted streams are marked
    status='disabled' instead of 'unknown'. Disabled streams are
    kept as a warm backup in the DB but never serve as winners
    (only status='online' streams win). The pruner leaves them
    alone because it only sweeps labels where all streams are
    'offline' — a label with all-disabled streams survives.
    """
    channels_new = 0
    streams_new = 0
    duplicates = 0

    initial_status = "disabled" if disabled else "unknown"

    for entry in entries:
        try:
            # `canonical_channel_name` first applies the operator's
            # multi-region override (e.g. "PBS Kids Alaska" → "pbs kids"),
            # then falls through to the regular normalizer. Two
            # region-flavored variants of the same network collapse
            # into one channels row with multiple stream URLs —
            # the failover list the player already walks.
            norm = canonical_channel_name(entry.name)
            if not norm:
                duplicates += 1
                continue

            # 1. Resolve the channel: tvg-id first, then normalized name.
            channel_id: Optional[int] = None

            if entry.tvg_id:
                async with db.execute(
                    "SELECT id FROM channels WHERE tvg_id = ?", (entry.tvg_id,)
                ) as cur:
                    row = await cur.fetchone()
                    if row:
                        channel_id = int(row["id"])

            if channel_id is None:
                async with db.execute(
                    "SELECT id FROM channels WHERE normalized_name = ?", (norm,)
                ) as cur:
                    row = await cur.fetchone()
                    if row:
                        channel_id = int(row["id"])

            # 2. Create the channel if it didn't exist.
            if channel_id is None:
                cur = await db.execute(
                    """
                    INSERT INTO channels
                        (normalized_name, display_name, tvg_id, tvg_name,
                         group_title, logo_url, bouquet)
                    VALUES (?, ?, ?, ?, ?, ?, 'Auto')
                    """,
                    (
                        norm,
                        entry.name.strip(),
                        entry.tvg_id,
                        entry.tvg_name,
                        entry.group_title or "Other",
                        entry.tvg_logo,
                    ),
                )
                channel_id = int(cur.lastrowid)
                channels_new += 1
            else:
                # Backfill missing fields opportunistically.
                await db.execute(
                    """
                    UPDATE channels
                    SET tvg_id = COALESCE(tvg_id, ?),
                        tvg_name = COALESCE(tvg_name, ?),
                        logo_url = COALESCE(logo_url, ?),
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (entry.tvg_id, entry.tvg_name, entry.tvg_logo, channel_id),
                )

            # 3. Insert the stream; UNIQUE(source_url) dedupes.
            cur = await db.execute(
                """
                INSERT OR IGNORE INTO streams
                    (channel_id, source_url, source_label, priority, status)
                VALUES (?, ?, ?, 100, ?)
                """,
                (channel_id, entry.url, source_label, initial_status),
            )
            if cur.lastrowid:
                streams_new += 1
            else:
                duplicates += 1
        except Exception as e:
            log.warning("Failed to import entry %r: %s", entry, e)
            continue

    await db.commit()
    return {
        "channels_new": channels_new,
        "streams_new": streams_new,
        "duplicates": duplicates,
    }
