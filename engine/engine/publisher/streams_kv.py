"""Cloudflare KV publisher for per-channel stream lists.

The public site's HLS player needs an *ordered list* of online stream
URLs for each channel so it can fail over when one source stalls or
returns 404. The existing `publish_redirects` writes only the single
winning URL per token (under the token key); this publisher writes
the full ordered list under `streams:<token>` for the player to
consume.

The Worker exposes this at /api/streams/<token>. The same diff-and-
skip, semaphore-bound, gather pattern as `publish_redirects` is
reused; the only differences are the SQL, the JSON payload, and the
KV key prefix.
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

from ..db import open_db
from .kv import _delete_kv, _get_kv, _put_kv

log = logging.getLogger("treefrog.kv")


async def publish_stream_lists(*, force: bool = False) -> dict:
    """For each `redirects` row, write `streams:<token>` to KV as JSON:

        {
          "channel_id": 12,
          "tvg_id":     "bbcnews.uk",
          "name":       "BBC News",
          "logo":       "https://...",
          "urls":       ["https://a/...m3u8", "https://b/...m3u8"]
        }

    The URLs come from the `streams` table for that channel with
    `status='online'`, ordered by `priority ASC, id ASC`. If a
    channel has zero online streams, its `streams:<token>` key is
    deleted (so the player sees a 404 and shows "no streams").

    Returns {written, deleted, unchanged, errors, total}.
    """
    db = await open_db()
    try:
        async with db.execute(
            """
            SELECT r.token          AS token,
                   c.id             AS channel_id,
                   c.display_name   AS name,
                   c.tvg_id         AS tvg_id,
                   c.logo_url       AS logo,
                   c.status         AS channel_status
            FROM redirects r
            JOIN channels c ON c.id = r.channel_id
            """
        ) as cur:
            rows = await cur.fetchall()

        # For each channel, pull its online streams ordered by priority.
        streams_by_channel: dict[str, list[str]] = {}
        for r in rows:
            async with db.execute(
                """
                SELECT source_url
                FROM streams
                WHERE channel_id = ? AND status = 'online'
                ORDER BY priority ASC, id ASC
                """,
                (r["channel_id"],),
            ) as cur:
                urls = [row["source_url"] for row in await cur.fetchall()]
            streams_by_channel[r["token"]] = urls
    finally:
        await db.close()

    written = deleted = unchanged = errors = 0
    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        sem = asyncio.Semaphore(20)

        async def process(token: str, channel_id: int, name: str,
                          tvg_id: str | None, logo: str | None,
                          channel_status: str, urls: list[str]) -> None:
            nonlocal written, deleted, unchanged, errors
            async with sem:
                # If the channel went offline, drop the stream list
                # entirely — there's nothing for the player to play.
                if channel_status != "online" or not urls:
                    if force or await _get_kv(session, f"streams:{token}") is not None:
                        if await _delete_kv(session, f"streams:{token}"):
                            deleted += 1
                        else:
                            errors += 1
                    else:
                        unchanged += 1
                    return
                payload = json.dumps(
                    {
                        "channel_id": channel_id,
                        "tvg_id": tvg_id,
                        "name": name,
                        "logo": logo,
                        "urls": urls,
                    },
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                if not force:
                    current = await _get_kv(session, f"streams:{token}")
                    if current == payload:
                        unchanged += 1
                        return
                if await _put_kv(session, f"streams:{token}", payload):
                    written += 1
                else:
                    errors += 1

        await asyncio.gather(
            *(
                process(
                    r["token"], int(r["channel_id"]), r["name"],
                    r["tvg_id"], r["logo"], r["channel_status"],
                    streams_by_channel[r["token"]],
                )
                for r in rows
            )
        )
    log.info(
        "KV stream lists: written=%d deleted=%d unchanged=%d errors=%d (of %d)",
        written, deleted, unchanged, errors, len(rows),
    )
    return {
        "written": written,
        "deleted": deleted,
        "unchanged": unchanged,
        "errors": errors,
        "total": len(rows),
    }
