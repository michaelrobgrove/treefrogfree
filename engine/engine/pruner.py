"""Dead-playlist pruner.

A "dead playlist" is a `source_label` whose streams are ALL offline.
The most common case is an upstream M3U that changed its URL shape,
went geo-blocked, or is generating malformed entries — the engine
imports the streams successfully but the health cycle never marks
any of them online. The pruner sweeps these out so the catalog
stays clean.

Safety: we only prune a label when every single one of its streams
is offline. A label with 100 streams where 99 are offline is left
alone (one live stream still means the playlist is "working"). We
also only delete channels that would become truly orphaned — a
channel fed by both "DistroTV US" and "Pluto US" survives even if
DistroTV US goes 100% offline, because the Pluto stream is still
backing it.

Schema-side, the cascades do most of the work:
  - `streams.channel_id` → `channels` is CASCADE channel→stream.
    Deleting a channel wipes its streams; deleting a stream does
    NOT touch the channel. We rely on the latter: the pruner
    deletes by source_label, so channels with multi-source streams
    are untouched.
  - `health_logs.stream_id` → CASCADE, so stream deletion wipes
    the rolling-availability history (good — we don't want a
    pruned stream's bad history to leak into a re-import).
  - `redirects.channel_id` and `redirects.stream_id` both CASCADE.
    When we delete orphan channels, their redirect tokens go too.
    publish_redirects on the next tick will repopulate tokens for
    any channels that still have an online stream.

Runs:
  - automatically at the end of every scheduler `_tick()`
  - on demand via `python -m engine prune [--dry-run]`
  - on demand via `POST /api/admin/prune[?dry_run=1]`
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

log = logging.getLogger("treefrog.pruner")


async def prune_dead_playlists(
    db,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Drop any source_label whose streams are ALL offline.

    Args:
        db: An open aiosqlite connection. Caller is responsible for
            commit/close.
        dry_run: If True, report what would be deleted without
            touching the database. We still open a transaction so
            the SELECTs see a consistent view.

    Returns:
        {
          "dry_run": bool,
          "scanned_labels": int,   # distinct source_labels with >0 streams
          "dead_labels":   int,    # labels that were (would be) pruned
          "pruned": [           # one entry per dead label
            {
              "source_label": str,
              "streams_deleted":  int,
              "channels_deleted": int,   # orphan channels dropped
            },
            ...
          ],
        }
    """
    # 1. Find labels whose streams are ALL offline (and there is at
    #    least one stream — protects against freshly-imported labels
    #    whose health check hasn't run yet; "unknown" status is not
    #    "offline" so those labels are not pruned).
    async with db.execute(
        """
        SELECT source_label,
               COUNT(*)                                         AS total_streams,
               SUM(CASE WHEN status = 'online'  THEN 1 ELSE 0 END) AS online_streams,
               SUM(CASE WHEN status = 'offline' THEN 1 ELSE 0 END) AS offline_streams
        FROM streams
        WHERE source_label IS NOT NULL
        GROUP BY source_label
        HAVING SUM(CASE WHEN status = 'offline' THEN 1 ELSE 0 END) > 0
           AND SUM(CASE WHEN status = 'online'  THEN 1 ELSE 0 END) = 0
        ORDER BY source_label
        """
    ) as cur:
        dead_labels = [dict(r) for r in await cur.fetchall()]

    # Total distinct labels (including healthy ones) for the summary.
    async with db.execute(
        "SELECT COUNT(DISTINCT source_label) AS n FROM streams WHERE source_label IS NOT NULL"
    ) as cur:
        scanned_row = await cur.fetchone()
    scanned_labels = int(scanned_row["n"]) if scanned_row else 0

    if not dead_labels:
        log.info(
            "prune: scanned %d label(s), nothing to prune",
            scanned_labels,
        )
        return {
            "dry_run": dry_run,
            "scanned_labels": scanned_labels,
            "dead_labels": 0,
            "pruned": [],
        }

    log.info(
        "prune: scanned %d label(s), found %d dead label(s)%s: %s",
        scanned_labels,
        len(dead_labels),
        " (dry run)" if dry_run else "",
        ", ".join(r["source_label"] for r in dead_labels),
    )

    pruned: List[Dict[str, Any]] = []

    for row in dead_labels:
        label: str = row["source_label"]
        total_streams: int = int(row["total_streams"])

        # 2. Find channels that would become orphaned by deleting this
        #    label's streams — i.e. channels that have streams from
        #    this label AND no streams from any other label (or with
        #    no label, which is the seed-from-curl case).
        #    DISTINCT matters: a channel with 2 dead-label streams
        #    would otherwise appear twice in the result, inflating
        #    the channels_deleted count.
        async with db.execute(
            """
            SELECT DISTINCT s.channel_id
            FROM streams s
            WHERE s.source_label = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM streams s2
                  WHERE s2.channel_id = s.channel_id
                    AND (s2.source_label IS NULL OR s2.source_label != ?)
              )
            """,
            (label, label),
        ) as cur:
            orphan_channel_ids = [int(r["channel_id"]) for r in await cur.fetchall()]

        if dry_run:
            pruned.append({
                "source_label": label,
                "streams_deleted": total_streams,
                "channels_deleted": len(orphan_channel_ids),
                "channels_orphan_ids_sample": orphan_channel_ids[:5],
            })
            continue

        # 3. Delete this label's streams. CASCADE wipes:
        #    - health_logs rows for each deleted stream
        #    - redirects rows that pointed at each deleted stream
        #    (deleting a stream does NOT cascade to channels, which
        #    is exactly the behavior we want.)
        await db.execute(
            "DELETE FROM streams WHERE source_label = ?",
            (label,),
        )

        # 4. Delete orphan channels. CASCADE wipes:
        #    - their remaining streams (none, by definition)
        #    - health_logs (none)
        #    - redirects rows for those channels
        channels_deleted = 0
        if orphan_channel_ids:
            placeholders = ",".join("?" * len(orphan_channel_ids))
            await db.execute(
                f"DELETE FROM channels WHERE id IN ({placeholders})",
                orphan_channel_ids,
            )
            channels_deleted = len(orphan_channel_ids)

        # 5. Annotate the imports row so the operator can see in the
        #    audit log which imports were pruned and when. We tag the
        #    most recent import for this label; previous imports are
        #    left as historical record.
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        prune_note = (
            f" [pruned: {total_streams} streams, {channels_deleted} "
            f"orphan channels at {now_iso}]"
        )
        await db.execute(
            """
            UPDATE imports
            SET notes = COALESCE(notes, '') || ?
            WHERE id = (
                SELECT id FROM imports
                WHERE source_label = ?
                ORDER BY id DESC
                LIMIT 1
            )
            """,
            (prune_note, label),
        )

        log.info(
            "prune: %s — deleted %d streams, %d orphan channel(s)",
            label,
            total_streams,
            channels_deleted,
        )
        pruned.append({
            "source_label": label,
            "streams_deleted": total_streams,
            "channels_deleted": channels_deleted,
        })

    await db.commit()

    return {
        "dry_run": dry_run,
        "scanned_labels": scanned_labels,
        "dead_labels": len(dead_labels),
        "pruned": pruned,
    }
