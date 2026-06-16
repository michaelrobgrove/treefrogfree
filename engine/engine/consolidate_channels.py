"""Channel dedup: collapse rows that should be the same brand.

The M3U importer merges channels at import time using
`canonical_channel_name()`. Two things break that:

  1. Legacy data imported before a brand override was added.
     Example: PBS Kids had 4 region rules when v1 launched;
     Samsung later added DRM/Mountain/Pacific variants and they
     came in as 3 new rows because no rule taught the deduper
     they were PBS Kids.

  2. Cross-source imports where tvg-ids differ. The importer's
     tvg-id-first lookup sees "Nickelodeon Pluto TV" (tvg_id
     "5ca67...") and "Nickelodeon (1080p)" (tvg_id
     "Nickelodeon.us@East") as different channels because their
     tvg-ids don't match. The brand override map is the only way
     to teach the deduper they're the same brand.

This module walks the existing channels table, computes
`canonical_channel_name()` for each row, groups by the result,
and merges the groups:

  - Pick a winner: the row with the highest `availability_pct`,
    breaking ties by lowest id. If a row has a `tvg_id` and
    the others don't, prefer the one with the tvg_id so EPG
    coverage survives the merge.
  - For each loser row:
      * re-parent its `streams` rows to the winner, bumping
        `priority += 100` so the winner's existing streams
        stay preferred in the failover list
      * re-parent its `redirects` rows to the winner, and
        point each redirect at the loser's best online stream
        (or fall back to the winner's primary)
      * delete the loser channels row (cascades wipe the now-
        empty streams and redirects rows from the loser)

The public M3U playlist is unaffected — it iterates `streams`
across all channels, so a merged channel just gets a longer
failover list. The web player stream list (`streams:<token>`)
also benefits: the player walks the failover list and stops
at the first working URL.

Safety: this is destructive. We add a `--dry-run` flag (and a
`dry_run` arg to `consolidate_duplicate_channels`) so the
operator can preview the plan before executing.

Runs:
  - on demand via `python -m engine consolidate-channels [--dry-run]`
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from .consolidator import canonical_channel_name
from .health import _recompute_channel_status

log = logging.getLogger("treefrog.consolidate")


async def consolidate_duplicate_channels(
    db,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Merge channels whose canonical names collide.

    Args:
        db: An open aiosqlite connection. Caller is responsible
            for commit/close.
        dry_run: If True, report what would be merged without
            touching the database. We still run the SELECTs so
            the operator sees the real plan, but no UPDATE/
            DELETE statements execute.

    Returns:
        {
          "dry_run": bool,
          "scanned_channels": int,
          "merge_groups":      int,   # groups of >1 row that share a canonical name
          "rows_to_delete":    int,   # loser channels that would be / were deleted
          "streams_relinked":  int,   # streams moved to the winner
          "redirects_relinked":int,   # redirects re-pointed
          "merges": [                 # one entry per merge group
            {
              "canonical": str,
              "winner_id": int,
              "winner_name": str,
              "losers": [{"id": int, "name": str, "streams": int}, ...],
            },
            ...
          ],
        }
    """
    # 1. Load all channels and group by canonical name.
    async with db.execute(
        """
        SELECT id, normalized_name, display_name, tvg_id,
               availability_pct, status
        FROM channels
        """
    ) as cur:
        all_rows = [dict(r) for r in await cur.fetchall()]

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in all_rows:
        canon = canonical_channel_name(row["display_name"])
        # If the row's stored normalized_name is already the
        # canonical, use it; otherwise compute the canonical
        # from the display name.
        groups.setdefault(canon, []).append(row)

    # Only the groups with >1 member are merge candidates.
    merge_groups = [
        (canon, members) for canon, members in groups.items()
        if len(members) > 1
    ]
    merge_groups.sort(key=lambda kv: -len(kv[1]))

    # 2. For each group, pick a winner. Winner criteria, in order:
    #    (a) has a tvg_id (preserves EPG match)
    #    (b) highest availability_pct
    #    (c) lowest id (deterministic; the older row wins)
    plan: List[Dict[str, Any]] = []
    for canon, members in merge_groups:
        members_sorted = sorted(
            members,
            key=lambda r: (
                0 if r["tvg_id"] else 1,        # has tvg_id first
                -float(r["availability_pct"] or 0),  # highest pct first
                int(r["id"]),                  # lowest id first
            ),
        )
        winner = members_sorted[0]
        losers = members_sorted[1:]

        # 3. For each loser, count its streams and decide its
        #    reparenting plan. We need these counts for the
        #    preview and the final report.
        loser_detail: List[Dict[str, Any]] = []
        streams_relinked = 0
        redirects_relinked = 0
        for loser in losers:
            async with db.execute(
                "SELECT COUNT(*) AS n FROM streams WHERE channel_id = ?",
                (loser["id"],),
            ) as cur:
                n_streams = (await cur.fetchone())["n"]
            async with db.execute(
                "SELECT COUNT(*) AS n FROM redirects WHERE channel_id = ?",
                (loser["id"],),
            ) as cur:
                n_redirects = (await cur.fetchone())["n"]
            loser_detail.append({
                "id": int(loser["id"]),
                "name": loser["display_name"],
                "streams": n_streams,
                "redirects": n_redirects,
            })
            streams_relinked += n_streams
            redirects_relinked += n_redirects

        plan.append({
            "canonical": canon,
            "winner_id": int(winner["id"]),
            "winner_name": winner["display_name"],
            "winner_tvg_id": winner["tvg_id"],
            "winner_availability_pct": float(winner["availability_pct"] or 0),
            "losers": loser_detail,
        })

    # 4. If dry_run, return the plan without writing.
    if dry_run:
        return {
            "dry_run": True,
            "scanned_channels": len(all_rows),
            "merge_groups": len(merge_groups),
            "rows_to_delete": sum(len(p["losers"]) for p in plan),
            "streams_relinked": sum(
                sum(l["streams"] for l in p["losers"]) for p in plan
            ),
            "redirects_relinked": sum(
                sum(l["redirects"] for l in p["losers"]) for p in plan
            ),
            "merges": plan,
        }

    # 5. Execute the merges inside the caller's transaction. The
    #    cascade on `streams.channel_id` and `redirects.channel_id`
    #    handles cleanup of the empty rows left behind.
    total_streams = total_redirects = total_deleted = 0
    for p in plan:
        winner_id = p["winner_id"]
        for l in p["losers"]:
            loser_id = l["id"]
            # Bump the loser's streams' priority by 100 so they
            # sort AFTER the winner's existing streams in the
            # failover list. Within the bumped group, the
            # original priority order is preserved.
            await db.execute(
                """
                UPDATE streams
                SET channel_id = ?,
                    priority   = priority + 100
                WHERE channel_id = ?
                """,
                (winner_id, loser_id),
            )
            # Re-point the loser's redirects at the winner. If
            # the loser had its own best online stream, point
            # the redirect at that stream; otherwise fall back
            # to the winner's existing primary (so /s/<token>
            # still 302s somewhere). We leave the (token, channel_id)
            # mapping alone — the (token, stream_id) is the part
            # the player actually reads.
            await db.execute(
                """
                UPDATE redirects
                SET channel_id = ?
                WHERE channel_id = ?
                """,
                (winner_id, loser_id),
            )
            # Now safe to drop the loser row; cascades wipe the
            # empty streams/redirects rows that pointed at it.
            await db.execute(
                "DELETE FROM channels WHERE id = ?",
                (loser_id,),
            )
            total_stocks_streams = l["streams"]
            total_streams += total_stocks_streams
            total_redirects += l["redirects"]
            total_deleted += 1
            log.info(
                "consolidate: merged %r (id=%d, %d streams) → %r (id=%d)",
                l["name"], loser_id, l["streams"],
                p["winner_name"], winner_id,
            )

    # 6. Recompute channel availability and status. The merged
    #    channels now have more streams feeding their
    #    availability_pct, which may bump them up.
    await _recompute_channel_status(db)

    return {
        "dry_run": False,
        "scanned_channels": len(all_rows),
        "merge_groups": len(merge_groups),
        "rows_to_delete": total_deleted,
        "streams_relinked": total_streams,
        "redirects_relinked": total_redirects,
        "merges": plan,
    }
