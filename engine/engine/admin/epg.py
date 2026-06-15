"""EPG: XMLTV import + channel mapping + output.

We fetch an XMLTV file, map `<channel>` ids onto our channels (tvg-id
first, normalized name fallback), and write the program data to disk.
The admin API serves the merged XMLTV on demand.

We don't store every program in SQLite — for v1 the file is small
enough to re-render from the cached XMLTV on disk. If catalog size
grows, swap in a parser that streams into a `programs` table.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import aiohttp

from ..config import CONFIG
from ..consolidator import normalize
from ..db import open_db

log = logging.getLogger("treefrog.epg")

EPG_DIR = CONFIG.data_dir / "epg"


def _epg_cache_path(url: str) -> Path:
    """Stable, hashed cache path for a fetched XMLTV file."""
    import hashlib
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return EPG_DIR / f"{h}.xml"


def _meta_path() -> Path:
    return EPG_DIR / "index.json"


def _load_index() -> dict:
    p = _meta_path()
    if not p.exists():
        return {"sources": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"sources": []}


def _save_index(idx: dict) -> None:
    _meta_path().write_text(json.dumps(idx, indent=2), encoding="utf-8")


async def import_epg_url(url: str) -> dict:
    """Fetch an XMLTV file, cache it, and (re)build the channel map."""
    EPG_DIR.mkdir(parents=True, exist_ok=True)
    cache = _epg_cache_path(url)
    timeout = aiohttp.ClientTimeout(total=120, connect=30)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=timeout) as resp:
            resp.raise_for_status()
            data = await resp.read()
    # Some hosts gzipped the .xml URL. Detect by magic bytes.
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    cache.write_bytes(data)
    log.info("Cached XMLTV: %s (%d bytes)", cache, len(data))

    # Rebuild channel index in the DB.
    channels_added = _index_channels_from_xml(cache)

    idx = _load_index()
    idx["sources"] = [s for s in idx["sources"] if s.get("url") != url]
    idx["sources"].append({
        "url": url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "bytes": len(data),
    })
    _save_index(idx)

    return {
        "url": url,
        "bytes": len(data),
        "channels_indexed": channels_added,
    }


def _index_channels_from_xml(xml_path: Path) -> int:
    """Walk the XMLTV <channel> elements and upsert into epg_channels.

    Returns the number of channels indexed.
    """
    db = open_db_sync_local()
    try:
        tree = ET.parse(str(xml_path))
        root = tree.getroot()
        count = 0
        for ch in root.findall("channel"):
            tvg_id = ch.get("id", "").strip()
            if not tvg_id:
                continue
            display = ch.findtext("display-name", default="", namespaces=None).strip() or None
            icon_el = ch.find("icon")
            icon = icon_el.get("src") if icon_el is not None else None
            db.execute(
                """
                INSERT INTO epg_channels (tvg_id, display_name, icon_url)
                VALUES (?, ?, ?)
                ON CONFLICT(tvg_id) DO UPDATE SET
                    display_name = COALESCE(excluded.display_name, epg_channels.display_name),
                    icon_url     = COALESCE(excluded.icon_url,     epg_channels.icon_url)
                """,
                (tvg_id, display, icon),
            )
            count += 1
        db.commit()
        return count
    finally:
        db.close()


def open_db_sync_local():
    """Local sync DB handle, mirrored from engine.db.open_db_sync but
    kept here to avoid an import cycle."""
    import sqlite3
    from ..config import CONFIG
    conn = sqlite3.connect(str(CONFIG.db_path))
    conn.row_factory = sqlite3.Row
    for pragma in (
        "PRAGMA journal_mode = WAL",
        "PRAGMA synchronous = NORMAL",
        "PRAGMA foreign_keys = ON",
    ):
        conn.execute(pragma)
    return conn


async def render_epg_xml() -> Optional[str]:
    """Read the most recent cached XMLTV, drop unmapped channels, return the XML.

    The output is rebuilt each call. We rewrite `channel id="..."` to the
    format our public site expects. The EPG is small enough to re-render
    on every request without caching at the engine layer (CF edge caches it).
    """
    EPG_DIR.mkdir(parents=True, exist_ok=True)
    cache_files = sorted(EPG_DIR.glob("*.xml"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cache_files:
        return None

    # Build a set of tvg-ids that map onto our channels.
    db = open_db_sync_local()
    try:
        cur = db.execute(
            """
            SELECT DISTINCT t.tvg_id
            FROM epg_channels t
            JOIN channels c ON c.tvg_id = t.tvg_id
            WHERE c.tvg_id IS NOT NULL
            """
        )
        mapped_by_id = {row["tvg_id"] for row in cur.fetchall()}

        cur = db.execute(
            "SELECT normalized_name FROM channels WHERE tvg_id IS NULL OR tvg_id = ''"
        )
        unkeyed_names = {row["normalized_name"] for row in cur.fetchall()}
    finally:
        db.close()

    # Stream-parse the XMLTV; rewrite channel ids and drop programs whose
    # channel isn't mapped. For v1 the file is small; we hold it in memory.
    tree = ET.parse(str(cache_files[0]))
    root = tree.getroot()

    keep_channels: set[str] = set()
    for ch in list(root.findall("channel")):
        tvg_id = (ch.get("id") or "").strip()
        if tvg_id in mapped_by_id:
            keep_channels.add(tvg_id)
            continue
        # Try name match against our unkeyed channels.
        display = ch.findtext("display-name", default="", namespaces=None) or ""
        if normalize(display) in unkeyed_names:
            keep_channels.add(tvg_id)
            continue
        # Not mapped: drop this channel + its programs.
        root.remove(ch)

    for prog in list(root.findall("programme")):
        ch = (prog.get("channel") or "").strip()
        if ch not in keep_channels:
            root.remove(prog)

    # Re-serialize. UTF-8 with XML declaration.
    return ET.tostring(root, encoding="unicode", xml_declaration=False)


async def import_epg_default() -> Optional[dict]:
    """Re-import all configured EPG sources. Called by the scheduler."""
    idx = _load_index()
    if not idx["sources"]:
        return None
    last_summary = None
    for src in idx["sources"]:
        try:
            last_summary = await import_epg_url(src["url"])
        except Exception as e:
            log.warning("EPG re-import failed for %s: %s", src["url"], e)
    return last_summary
