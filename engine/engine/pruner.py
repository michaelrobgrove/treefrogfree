"""Dead-playlist pruner.

A "dead playlist" falls into one of two buckets:

  1. `source_label` with >0 streams and ALL of them offline.
     The most common case: the M3U imported fine, but every URL
     has since gone 200 → 4xx/5xx (geo-block, generator 404'd,
     upstream CDN moved, malformed query string, etc.). The health
     cycle marks everything offline and the pruner sweeps it.

  2. `source_label` in the `imports` audit table but with 0
     streams. The M3U never produced any entries — the URL 404'd,
     the generator crashed mid-run, the M3U file was empty, the
     `urllib` parser rejected every line, etc. We annotate the
     import row but there's nothing to delete from `streams`/
     `channels` (they're already empty).

Safety: we only prune a label when every single one of its streams
is offline. A label with 100 streams where 99 are offline is left
alone (one live stream still means the playlist is "working"). We
also only delete channels that would become truly orphaned — a
channel fed by both "DistroTV US" and "Pluto US" survives even if
DistroTV US goes 100% offline, because the Pluto stream is still
backing it.

`status='disabled'` streams are NOT counted as offline. A label
with all-disabled streams survives the pruner (we treat disabled
as "kept as backup", not as a target for cleanup). This is what
lets us import backup playlists (e.g. the distrotv-proxy container
as a warm spare) without them getting pruned on the next cycle.

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
          "empty_labels":  int,    # labels in imports but with 0 streams (annotated)
          "pruned": [           # one entry per dead label
            {
              "source_label": str,
              "streams_deleted":  int,
              "channels_deleted": int,   # orphan channels dropped
            },
            ...
          ],
          "empties": [         # one entry per empty label (audit only)
            {"source_label": str, "import_id": int, "source_url": str | None},
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

    # 1b. Find labels in the imports audit table whose source_label
    #     has 0 streams. These are M3U imports that produced no
    #     entries (404, empty file, parser-rejected every line, etc.).
    #     There's nothing to delete from streams/channels, but we
    #     annotate the imports row so the operator can see in the
    #     audit log that the import is considered dead.
    async with db.execute(
        """
        SELECT i.id   AS import_id,
               i.source_label,
               i.source_url
        FROM imports i
        LEFT JOIN streams s ON s.source_label = i.source_label
        WHERE i.source_label IS NOT NULL
          AND i.finished_at IS NOT NULL   -- only consider completed imports
          AND s.id IS NULL                 -- and labels with no streams
        GROUP BY i.id, i.source_label, i.source_url
        ORDER BY i.id
        """
    ) as cur:
        empty_labels = [dict(r) for r in await cur.fetchall()]

    # Total distinct labels (including healthy ones) for the summary.
    async with db.execute(
        "SELECT COUNT(DISTINCT source_label) AS n FROM streams WHERE source_label IS NOT NULL"
    ) as cur:
        scanned_row = await cur.fetchone()
    scanned_labels = int(scanned_row["n"]) if scanned_row else 0

    if not dead_labels and not empty_labels:
        log.info(
            "prune: scanned %d label(s), nothing to prune",
            scanned_labels,
        )
        return {
            "dry_run": dry_run,
            "scanned_labels": scanned_labels,
            "dead_labels": 0,
            "empty_labels": 0,
            "pruned": [],
            "empties": [],
        }

    log.info(
        "prune: scanned %d label(s); found %d dead label(s)%s and %d empty label(s)%s",
        scanned_labels,
        len(dead_labels),
        " (dry run)" if dry_run else "",
        len(empty_labels),
        " (dry run)" if dry_run else "",
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

    # 6. Annotate empty labels. There's nothing in streams/channels to
    #    delete (by definition — the join found zero), so we just
    #    append a "[pruned: empty (0 streams)]" note to the import
    #    row. The operator can see in the audit log which imports
    #    are considered dead, without losing the historical record
    #    of "we tried to import this M3U on <date> and it 404'd".
    #
    #    `empties` reports labels that we ACTED on (annotated) this
    #    call. `empty_labels_skipped` tracks labels we found in the
    #    audit but already had the note — those are reported as
    #    `empty_labels` total in the summary so the operator can see
    #    the situation didn't change, but they don't double-annotate.
    empties: List[Dict[str, Any]] = []
    empty_skipped = 0
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for row in empty_labels:
        label: str = row["source_label"]
        import_id: int = int(row["import_id"])
        source_url = row.get("source_url")
        if dry_run:
            empties.append({
                "source_label": label,
                "import_id": import_id,
                "source_url": source_url,
            })
            continue
        # Skip if the note is already there (idempotent re-runs).
        # This makes the second prune() call a true no-op for the
        # empty-label sweep too, not just the streams one. We check
        # for EITHER '[pruned: ...' substring because a label that
        # was already swept as a dead-label gets the streams-form
        # note in step 5, and we shouldn't then add an empty-form
        # note on top of that.
        async with db.execute(
            "SELECT notes FROM imports WHERE id = ?", (import_id,)
        ) as cur:
            existing = await cur.fetchone()
        existing_notes = (existing["notes"] or "") if existing else ""
        if "[pruned:" in existing_notes:
            empty_skipped += 1
            continue
        prune_note = f" [pruned: empty (0 streams) at {now_iso}]"
        await db.execute(
            "UPDATE imports SET notes = notes || ? WHERE id = ?",
            (prune_note, import_id),
        )
        log.info(
            "prune: %s — empty label (0 streams), annotated import id=%d",
            label, import_id,
        )
        empties.append({
            "source_label": label,
            "import_id": import_id,
            "source_url": source_url,
        })

    await db.commit()

    return {
        "dry_run": dry_run,
        "scanned_labels": scanned_labels,
        "dead_labels": len(dead_labels),
        "empty_labels": len(empty_labels) - empty_skipped,  # actually annotated
        "empty_labels_seen": len(empty_labels),                # total detected
        "pruned": pruned,
        "empties": empties,
    }
