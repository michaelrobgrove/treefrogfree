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
      "tvg_id": "bbcnews.uk"
    }
  ]
}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from ..config import CONFIG
from ..consolidator import group_brand_name, group_slug
from ..db import open_db

log = logging.getLogger("treefrog.catalog")


async def build_catalog() -> dict:
    db = await open_db()
    try:
        async with db.execute(
            """
            SELECT id, display_name, tvg_id, group_title, logo_url,
                   availability_pct
            FROM channels
            WHERE status = 'online'
            ORDER BY group_title, display_name
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
                    "name": group_brand_name(g["group_title"]),
                    "slug": group_slug(g["group_title"]),
                    "count": int(g["n"]),
                }
                for g in groups
            ],
            key=lambda c: c["name"],
        )

        out_channels = [
            {
                "id": int(c["id"]),
                "name": c["display_name"],
                "category": group_slug(c["group_title"]),
                "logo": c["logo_url"],
                "availability_pct": round(float(c["availability_pct"] or 0), 1),
                "tvg_id": c["tvg_id"],
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
