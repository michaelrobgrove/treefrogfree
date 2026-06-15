"""Generate the public channel/category JSON for the static site.

The site loads this file client-side and renders the channel grid + search
results without a round trip. We aim for < 200 KB gzipped at v1 scale.

Shape (intentionally simple — easy to consume from vanilla JS):
{
  "generated_at": "2026-06-15T12:00:00Z",
  "stats": {
    "channel_count": 1274,
    "category_count": 42,
    "average_availability_pct": 98.3,
    "last_health_check": "2026-06-15T11:30:00Z"
  },
  "categories": [
    {"name": "🐸 Tree Frog Free | News", "slug": "news", "count": 87}
  ],
  "channels": [
    {
      "id": 12,
      "name": "BBC News",
      "category": "news",
      "logo": "http://img/bbc.png",
      "availability_pct": 99.7,
      "tvg_id": "bbcnews.uk",
      "token": "a1b2c3d4..."        // null if no online stream
    }
  ]
}

The `token` field is the redirect token under which the winning stream URL
(plus the full failover list at /api/streams/<token>) lives in KV. The
public HLS player uses it to start playback without a follow-up redirect
lookup. Channels with no online stream have `token: null` and the player
falls back to the existing detail page.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from ..config import CONFIG
from ..consolidator import canonical_category, group_brand_name, group_slug
from ..db import open_db

log = logging.getLogger("treefrog.catalog")


async def build_catalog() -> dict:
    db = await open_db()
    try:
        async with db.execute(
            """
            SELECT c.id, c.display_name, c.tvg_id, c.group_title, c.logo_url,
                   c.availability_pct,
                   (SELECT r.token
                    FROM redirects r
                    JOIN streams s ON s.id = r.stream_id
                    WHERE r.channel_id = c.id
                      AND s.status     = 'online'
                    LIMIT 1)          AS token
            FROM channels c
            WHERE c.status = 'online'
            ORDER BY c.group_title, c.display_name
            """
        ) as cur:
            channels = await cur.fetchall()

        async with db.execute(
            """
            SELECT group_title, COUNT(*) AS n,
                   AVG(availability_pct) AS avg_avail,
                   MAX(last_checked_at) AS last_check
            FROM channels
            WHERE status = 'online'
            GROUP BY group_title
            """
        ) as cur:
            groups = await cur.fetchall()

        # Coalesce the raw group_title (often 70+ distinct values
        # from a free M3U) into the canonical ~15-20 the UI actually
        # shows. The canonical name drives both the categories list
        # display and the per-channel `category` slug, so two raw
        # M3U titles like "Animation Classics" and "Animation Kids"
        # both surface as the "Animation" pill.
        canonical_groups: dict[str, dict] = {}  # canonical_name -> {n, avail_sum, avail_n, last_check}
        for g in groups:
            canon = canonical_category(g["group_title"])
            bucket = canonical_groups.setdefault(
                canon,
                {"n": 0, "avail_sum": 0.0, "avail_n": 0, "last_check": "1970-01-01T00:00:00Z"},
            )
            bucket["n"] += int(g["n"])
            if g["avg_avail"] is not None:
                bucket["avail_sum"] += float(g["avg_avail"]) * int(g["n"])
                bucket["avail_n"] += int(g["n"])
            if g["last_check"] and g["last_check"] > bucket["last_check"]:
                bucket["last_check"] = g["last_check"]
        # Reshape into the dict shape the rest of the function expects.
        merged_groups = [
            {
                "canonical": canon,
                "n": data["n"],
                "avg_avail": (data["avail_sum"] / data["avail_n"]) if data["avail_n"] else None,
                "last_check": data["last_check"],
            }
            for canon, data in canonical_groups.items()
        ]
        groups = merged_groups

        last_check = "1970-01-01T00:00:00Z"
        total_avail = 0.0
        n_avail = 0
        for g in groups:
            if g["last_check"] and g["last_check"] > last_check:
                last_check = g["last_check"]
            if g["avg_avail"] is not None:
                total_avail += float(g["avg_avail"]) * int(g["n"])
                n_avail += int(g["n"])

        stats = {
            "channel_count": len(channels),
            "category_count": len(groups),
            "average_availability_pct": round(total_avail / n_avail, 1) if n_avail else 0.0,
            "last_health_check": _to_iso(last_check),
        }

        categories = sorted(
            [
                {
                    "name": group_brand_name(g["canonical"]),
                    "slug": group_slug(g["canonical"]),
                    "count": int(g["n"]),
                }
                for g in groups
            ],
            key=lambda c: c["name"],
        )

        # Per-channel `category` is the canonical slug, not the raw
        # group_title. This is what the grid's category filter
        # matches against, so two channels from different raw M3U
        # groups ("Animation Classics" + "Animation Kids") both
        # surface under the "animation" filter pill.
        out_channels = [
            {
                "id": int(c["id"]),
                "name": c["display_name"],
                "category": group_slug(canonical_category(c["group_title"])),
                "logo": c["logo_url"],
                "availability_pct": round(float(c["availability_pct"] or 0), 1),
                "tvg_id": c["tvg_id"],
                "token": c["token"],
            }
            for c in channels
        ]

        return {
            "generated_at": _to_iso(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
            "stats": stats,
            "categories": categories,
            "channels": out_channels,
        }
    finally:
        await db.close()


def _to_iso(s: str) -> str:
    """Convert a SQLite 'YYYY-MM-DD HH:MM:SS' timestamp to ISO 8601 with Z."""
    if not s or s == "1970-01-01T00:00:00Z":
        return s
    try:
        # Treat as UTC.
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return s


async def write_catalog() -> str:
    catalog = await build_catalog()
    out = CONFIG.public_dir / "channels.json"
    out.write_text(json.dumps(catalog, separators=(",", ":")), encoding="utf-8")
    log.info(
        "Wrote catalog: %d channels, %d categories → %s",
        catalog["stats"]["channel_count"],
        catalog["stats"]["category_count"],
        out,
    )
    return str(out)
