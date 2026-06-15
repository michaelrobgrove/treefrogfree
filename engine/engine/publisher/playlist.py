"""Generate the public-facing M3U playlist.

Branding rules (plan.md §3.4):
- Channel display_name is preserved exactly as the source provided it.
- Group titles are prefixed with "🐸 Tree Frog Free | ".
- The playlist's stream URL points at /s/<token> — the actual source URL
  is resolved by the Cloudflare Worker. The token is stable per channel
  and gets assigned on first publish.
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional

from ..config import CONFIG
from ..consolidator import group_brand_name
from ..db import open_db

log = logging.getLogger("treefrog.playlist")

# 6 chars, lowercase alphanumeric. 36^6 = ~2.2 billion combinations.
# Plenty for thousands of channels, brute-force-impractical for scrapers.
_TOKEN_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def _mint_token() -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(6))


async def _ensure_redirect(db, channel_id: int, stream_id: int) -> Optional[str]:
    """Return the existing token for a channel, minting one if needed.

    The token is bound to the current winning stream_id. We update the
    binding whenever the winner changes; see _update_redirect_target.
    """
    async with db.execute(
        "SELECT token, stream_id FROM redirects WHERE channel_id = ?", (channel_id,)
    ) as cur:
        row = await cur.fetchone()
    if row:
        if row["stream_id"] != stream_id:
            await _update_redirect_target(db, row["token"], stream_id)
        return row["token"]
    # Mint with a tiny collision check. With 36^6 keyspace, collisions
    # are not a real concern at v1 scale, but check anyway.
    for _ in range(5):
        token = _mint_token()
        async with db.execute(
            "SELECT 1 FROM redirects WHERE token = ?", (token,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await db.execute(
                "INSERT INTO redirects (token, channel_id, stream_id) VALUES (?, ?, ?)",
                (token, channel_id, stream_id),
            )
            return token
    raise RuntimeError("Could not mint a unique redirect token")


async def _select_winner_stream(db, channel_id: int) -> Optional[dict]:
    """Pick the lowest-priority online stream for a channel."""
    async with db.execute(
        """
        SELECT s.id AS stream_id, s.source_url, s.priority
        FROM streams s
        WHERE s.channel_id = ? AND s.status = 'online'
        ORDER BY s.priority ASC, s.id ASC
        LIMIT 1
        """,
        (channel_id,),
    ) as cur:
        return await cur.fetchone()


async def _update_redirect_target(db, token: str, stream_id: int) -> None:
    """Bind the redirect token to the current winning stream."""
    await db.execute(
        """
        UPDATE redirects
        SET stream_id = ?, updated_at = datetime('now')
        WHERE token = ?
        """,
        (stream_id, token),
    )


async def _build_redirect_map(db) -> dict[int, str]:
    """For each channel that has an online stream, ensure a redirect token
    points at the current winner. Returns {channel_id: token}."""
    async with db.execute(
        """
        SELECT c.id AS channel_id
        FROM channels c
        WHERE c.status = 'online'
        ORDER BY c.id
        """
    ) as cur:
        channels = await cur.fetchall()

    token_by_channel: dict[int, str] = {}
    for ch in channels:
        cid = ch["channel_id"]
        winner = await _select_winner_stream(db, cid)
        if not winner:
            continue
        token = await _ensure_redirect(db, cid, int(winner["stream_id"]))
        token_by_channel[cid] = token
    await db.commit()
    return token_by_channel


async def _resolve_public_base() -> str:
    """Base URL used in the M3U. Falls back to a relative path
    (which most players handle) if no env var is set."""
    import os
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    return base


async def render_playlist(*, bouquet: Optional[str] = None) -> str:
    """Render the public M3U playlist to a string.

    If `bouquet` is given, only channels in that bouquet are included.
    """
    base = await _resolve_public_base()
    db = await open_db()
    try:
        token_by_channel = await _build_redirect_map(db)

        if bouquet:
            sql = """
                SELECT id, display_name, tvg_id, tvg_name, group_title, logo_url
                FROM channels
                WHERE status = 'online' AND bouquet = ?
                ORDER BY group_title, display_name
            """
            params: tuple = (bouquet,)
        else:
            sql = """
                SELECT id, display_name, tvg_id, tvg_name, group_title, logo_url
                FROM channels
                WHERE status = 'online'
                ORDER BY group_title, display_name
            """
            params = ()

        async with db.execute(sql, params) as cur:
            channels = await cur.fetchall()

        lines = ["#EXTM3U"]
        for ch in channels:
            token = token_by_channel.get(ch["id"])
            if not token:
                continue  # no online stream; skip
            url = f"{base}/s/{token}" if base else f"/s/{token}"
            attrs = []
            if ch["tvg_id"]:
                attrs.append(f'tvg-id="{_xml_attr(ch["tvg_id"])}"')
            if ch["tvg_name"]:
                attrs.append(f'tvg-name="{_xml_attr(ch["tvg_name"])}"')
            else:
                attrs.append(f'tvg-name="{_xml_attr(ch["display_name"])}"')
            if ch["logo_url"]:
                attrs.append(f'tvg-logo="{_xml_attr(ch["logo_url"])}"')
            attrs.append(
                f'group-title="{_xml_attr(group_brand_name(ch["group_title"]))}"'
            )
            attr_str = " ".join(attrs)
            lines.append(f"#EXTINF:-1 {attr_str},{ch['display_name']}")
            lines.append(url)

        return "\n".join(lines) + "\n"
    finally:
        await db.close()


def _xml_attr(s: str) -> str:
    """Escape a value for an M3U attribute. Players vary in strictness;
    quotes and backslashes are the common pitfalls."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


async def write_playlist(*, bouquet: Optional[str] = None) -> str:
    """Render the playlist to disk. Returns the path written."""
    content = await render_playlist(bouquet=bouquet)
    out_dir = CONFIG.public_dir
    suffix = f"-{bouquet.lower()}" if bouquet else ""
    path = out_dir / f"playlist{suffix}.m3u"
    path.write_text(content, encoding="utf-8")
    log.info("Wrote %d-line playlist to %s", content.count("\n"), path)
    return str(path)
