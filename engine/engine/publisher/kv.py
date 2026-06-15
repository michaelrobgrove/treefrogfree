"""Cloudflare KV publisher.

After every health cycle (or on demand), we push the winning stream URL
for each channel to Cloudflare KV. The Worker reads from KV on every
/s/<token> hit, so KV is the only place that knows the live source URL.

Free tier limits: 100K reads/day, 1K writes/sec. We only write on
*change* (see _diff_and_write), so write volume stays small.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import aiohttp

from ..config import CONFIG
from ..db import open_db

log = logging.getLogger("treefrog.kv")


def _cf_url(namespace_id: str, *, account_id: str = "") -> str:
    """Cloudflare KV put URL. Account ID is bound at deploy time via env."""
    return (
        f"https://api.cloudflare.com/client/v4/accounts/{CONFIG.cf_account_id}"
        f"/storage/kv/namespaces/{namespace_id}/values/"
    )


async def _put_kv(session: aiohttp.ClientSession, key: str, value: str) -> bool:
    """Write a single key to KV. Returns True on success."""
    if not (CONFIG.cf_api_token and CONFIG.cf_account_id and CONFIG.cf_kv_namespace_id):
        log.warning("CF credentials missing; skipping KV write for %s", key)
        return False
    url = _cf_url(CONFIG.cf_kv_namespace_id) + key
    headers = {
        "Authorization": f"Bearer {CONFIG.cf_api_token}",
        "Content-Type": "text/plain",
    }
    try:
        async with session.put(url, headers=headers, data=value, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return True
            body = await resp.text()
            log.warning("KV PUT %s failed: %s %s", key, resp.status, body[:200])
            return False
    except aiohttp.ClientError as e:
        log.warning("KV PUT %s error: %s", key, e)
        return False


async def _get_kv(session: aiohttp.ClientSession, key: str) -> Optional[str]:
    """Read a single key from KV. Used by _diff_and_write to skip no-op writes."""
    if not (CONFIG.cf_api_token and CONFIG.cf_account_id and CONFIG.cf_kv_namespace_id):
        return None
    url = _cf_url(CONFIG.cf_kv_namespace_id) + key
    headers = {"Authorization": f"Bearer {CONFIG.cf_api_token}"}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return await resp.text()
            return None
    except aiohttp.ClientError:
        return None


async def _delete_kv(session: aiohttp.ClientSession, key: str) -> bool:
    """Delete a key from KV. Used when a channel is removed or all streams die."""
    if not (CONFIG.cf_api_token and CONFIG.cf_account_id and CONFIG.cf_kv_namespace_id):
        return False
    url = _cf_url(CONFIG.cf_kv_namespace_id) + key
    headers = {"Authorization": f"Bearer {CONFIG.cf_api_token}"}
    try:
        async with session.delete(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            return resp.status in (200, 204)
    except aiohttp.ClientError:
        return False


async def publish_public_assets(*, force: bool = False) -> dict:
    """Publish the public catalog and playlist to Cloudflare KV.

    The Worker reads these from KV on every /api/channels.json and
    /playlist.m3u request, so the engine never needs to be reachable
    from the public internet — only the admin UI (over Tailscale) hits
    the engine directly.

    Keys written:
        catalog:channels.json   — the public channel catalog (JSON)
        catalog:playlist.m3u    — the public M3U playlist

    Returns {written, unchanged, errors} for the two keys.
    """
    # Imported here to avoid a circular import at module load
    # (json_catalog and playlist import config, which can import db).
    from .json_catalog import build_catalog
    from .playlist import render_playlist

    catalog = await build_catalog()
    playlist = await render_playlist()

    catalog_json = json.dumps(catalog, separators=(",", ":"), ensure_ascii=False)
    playlist_str = playlist  # already a string

    written = unchanged = errors = 0
    connector = aiohttp.TCPConnector(limit=5)
    async with aiohttp.ClientSession(connector=connector) as session:
        for key, value in (
            ("catalog:channels.json", catalog_json),
            ("catalog:playlist.m3u", playlist_str),
        ):
            if not force:
                current = await _get_kv(session, key)
                if current == value:
                    unchanged += 1
                    log.info("KV public asset %s unchanged (skip)", key)
                    continue
            if await _put_kv(session, key, value):
                written += 1
                log.info("KV public asset %s written (%d bytes)", key, len(value))
            else:
                errors += 1
    return {"written": written, "unchanged": unchanged, "errors": errors}


async def publish_redirects(*, force: bool = False) -> dict:
    """Reconcile the engine's redirects table to Cloudflare KV.

    For each row in `redirects`:
      - If the winning stream URL has changed (or `force=True`), PUT the new value.
      - If the channel is now offline (no winning stream), DELETE the key.

    Returns {written, deleted, unchanged, errors}.
    """
    db = await open_db()
    try:
        async with db.execute(
            """
            SELECT r.token, r.stream_id, s.source_url, s.status, c.status AS channel_status
            FROM redirects r
            JOIN streams s ON s.id = r.stream_id
            JOIN channels c ON c.id = r.channel_id
            """
        ) as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()

    written = deleted = unchanged = errors = 0
    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        sem = asyncio.Semaphore(20)

        async def process(token, stream_id, source_url, stream_status, channel_status):
            nonlocal written, deleted, unchanged, errors
            async with sem:
                if channel_status != "online" or stream_status != "online":
                    # Channel or stream is down — drop the redirect.
                    if force or await _get_kv(session, token) is not None:
                        if await _delete_kv(session, token):
                            deleted += 1
                        else:
                            errors += 1
                    else:
                        unchanged += 1
                    return
                # Stream is online — compare against current KV value.
                if not force:
                    current = await _get_kv(session, token)
                    if current == source_url:
                        unchanged += 1
                        return
                if await _put_kv(session, token, source_url):
                    written += 1
                else:
                    errors += 1

        await asyncio.gather(
            *(
                process(r["token"], r["stream_id"], r["source_url"], r["status"], r["channel_status"])
                for r in rows
            )
        )
    log.info(
        "KV publish: written=%d deleted=%d unchanged=%d errors=%d (of %d)",
        written, deleted, unchanged, errors, len(rows),
    )
    return {"written": written, "deleted": deleted, "unchanged": unchanged, "errors": errors, "total": len(rows)}
