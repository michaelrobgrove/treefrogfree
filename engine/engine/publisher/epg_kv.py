"""Cloudflare KV publisher for EPG "now / next" JSON.

The HLS web player on the public site shows a small "On now" / "Up
next" panel next to the video when a channel's tvg-id maps onto a
programme in the imported XMLTV. The Worker exposes this at
/api/epg/nownext/<tvg_id>.

Rather than re-parse the XMLTV on every Worker read (it can be
megabytes and CF Workers have a 10 ms CPU budget per request), the
engine pre-computes a tiny JSON blob per mapped tvg-id and stores it
in the existing STREAM_KV namespace at `epg:nownext:<tvg_id>`:

    {
      "tvg_id":      "bbcnews.uk",
      "generated_at": "2026-06-15T18:00:00+00:00",
      "now":  { "title": "BBC News at Six", "start": "...", "stop": "..." } | null,
      "next": { "title": "The One Show",     "start": "...", "stop": "..." } | null
    }

`tvg_ids` that don't have a mapping to a `channels` row are skipped
(we don't publish a key for them; the player 404s and hides the
section).

### Tvg-id matching

The M3U's tvg-id is the source of truth for the channel — we never
rewrite it. But the M3U's tvg-id often has a quality suffix that the
EPG feed doesn't (e.g. `WGBATV261.us@HD` vs. the EPG's `WGBATV261.us`).
We resolve the EPG lookup with this strategy, in order:

  1. Exact match: M3U's tvg-id == EPG's tvg-id.
  2. @suffix strip: `WGBATV261.us@HD` -> `WGBATV261.us` and try
     again. We strip a known set of quality suffixes
     (`@HD`, `@SD`, `@FHD`, `@4K`, `@HDTV`, `@US`, `@USA`,
     `@UK`, `@CA`, `@MX`, `@EU`, `@LATAM`, `@WEB`, `@LIVE`).
  3. (Reserved for future) display-name fuzzy match.

The KV key always uses the *original* M3U tvg-id (so the player can
do `/api/epg/nownext/<channels.tvg_id>` and get a hit), and the
`compute_nownext` payload is taken from the EPG tvg-id that the
matcher selected.

The publish step runs after every EPG re-import (every 6h via the
scheduler) and is also exposed via the `publish` CLI subcommand and
the admin /rebuild-kv handler.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import aiohttp

from ..admin.epg import EPG_DIR
from ..db import open_db
from .kv import _put_kv

log = logging.getLogger("treefrog.kv")

# Quality / region suffixes that M3U tvg-ids often carry and that the
# EPG feed usually omits. We strip them when looking for an EPG match.
# Tweak with care — adding short common letters like `@a` would
# produce false positives.
_TVG_QUALITY_SUFFIXES = re.compile(
    r"@(HD|SD|FHD|4K|HDTV|US|USA|UK|CA|MX|EU|LATAM|WEB|LIVE)$",
    re.IGNORECASE,
)


def _strip_quality_suffix(tvg_id: str) -> str:
    """Remove a known quality / region suffix from a tvg-id. Returns
    the input unchanged if no suffix matches."""
    return _TVG_QUALITY_SUFFIXES.sub("", tvg_id)


@dataclass(frozen=True)
class EpgProgram:
    """One <programme> entry as it appears in the JSON we publish."""
    title: str
    start: str  # ISO 8601 with offset
    stop: str   # ISO 8601 with offset


def _parse_xmltv_dt(s: str) -> Optional[datetime]:
    """XMLTV uses `YYYYMMDDHHMMSS +0000` (no separators, fixed-width
    offset). Python's fromisoformat can't parse that until 3.11+, and
    not all our deploys are 3.11+. Format it explicitly."""
    if not s:
        return None
    s = s.strip()
    # XMLTV allows an optional ' +0000' offset; some feeds omit it.
    if len(s) >= 15 and s[:14].isdigit():
        try:
            dt = datetime(
                int(s[0:4]), int(s[4:6]), int(s[6:8]),
                int(s[8:10]), int(s[10:12]), int(s[12:14]),
                tzinfo=timezone.utc,
            )
            return dt
        except ValueError:
            return None
    # Fallback: try ISO 8601 (some feeds publish that).
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _format_iso(dt: datetime) -> str:
    """Format with explicit UTC offset, no microseconds. Stable across
    Python versions; the Worker's JSON parser will round-trip it."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _programs_for_channel(
    xml_root: ET.Element, tvg_id: str
) -> list[tuple[datetime, datetime, str]]:
    """Return [(start, stop, title), ...] for one channel, sorted by
    start time. Skips programmes with unparseable start/stop."""
    out: list[tuple[datetime, datetime, str]] = []
    for prog in xml_root.findall("programme"):
        if (prog.get("channel") or "").strip() != tvg_id:
            continue
        start = _parse_xmltv_dt(prog.get("start") or "")
        stop = _parse_xmltv_dt(prog.get("stop") or "")
        if start is None or stop is None:
            continue
        title_el = prog.find("title")
        title = (title_el.text or "").strip() if title_el is not None else ""
        out.append((start, stop, title))
    out.sort(key=lambda t: t[0])
    return out


def compute_nownext(
    xml_path: Path, tvg_ids: set[str], now: datetime
) -> dict[str, dict]:
    """Pure function: read one XMLTV file, compute {tvg_id: nownext}
    for each requested tvg_id. Unit-testable without DB or KV.

    `now` is taken as a parameter (not `datetime.now()`) so tests can
    pin a specific instant.

    A tvg_id with no programs returns `{now: None, next: None}` so the
    caller knows we considered it. A tvg_id that has no <programme>
    matching at all also gets that empty entry — useful for the player
    to know "we know about this channel, it's just off-air."
    """
    try:
        tree = ET.parse(str(xml_path))
    except Exception as e:
        log.warning("compute_nownext: failed to parse %s: %s", xml_path, e)
        return {tid: _empty() for tid in tvg_ids}
    root = tree.getroot()

    result: dict[str, dict] = {}
    for tid in tvg_ids:
        progs = _programs_for_channel(root, tid)
        result[tid] = _pick_now_next(progs, now)
    return result


def _empty() -> dict:
    return {"now": None, "next": None}


def _pick_now_next(
    progs: list[tuple[datetime, datetime, str]], now: datetime
) -> dict:
    """Given a channel's programmes sorted by start, find the one
    currently airing and the next one. Pure function — easy to test."""
    if not progs:
        return _empty()
    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)

    current = None
    next_idx = None
    for i, (start, stop, _title) in enumerate(progs):
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if stop.tzinfo is None:
            stop = stop.replace(tzinfo=timezone.utc)
        if start <= now_utc < stop:
            current = (start, stop, progs[i][2])
            # The next programme is the one *after* this one.
            if i + 1 < len(progs):
                nxt = progs[i + 1]
                nxt_start, nxt_stop = nxt[0], nxt[1]
                if nxt_start.tzinfo is None:
                    nxt_start = nxt_start.replace(tzinfo=timezone.utc)
                if nxt_stop.tzinfo is None:
                    nxt_stop = nxt_stop.replace(tzinfo=timezone.utc)
                next_idx = (nxt_start, nxt_stop, nxt[2])
            break
        elif start > now_utc:
            # First future programme we hit.
            next_idx = (start, stop, progs[i][2])
            break

    return {
        "now": asdict(EpgProgram(
            title=current[2], start=_format_iso(current[0]), stop=_format_iso(current[1])
        )) if current else None,
        "next": asdict(EpgProgram(
            title=next_idx[2], start=_format_iso(next_idx[0]), stop=_format_iso(next_idx[1])
        )) if next_idx else None,
    }


def _most_recent_xml() -> Optional[Path]:
    """The freshest cached XMLTV file in EPG_DIR. EPG_DIR is created
    on first import; if empty, return None."""
    EPG_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(EPG_DIR.glob("*.xml"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


async def publish_nownext(*, force: bool = False) -> dict:
    """Compute now/next for every tvg-id that maps to a channel and
    write `epg:nownext:<tvg_id>` JSON to KV.

    `force=True` writes even if the value is unchanged (useful from
    the rebuild-kv admin handler when the operator wants to be sure
    KV is current). For periodic re-imports, leave it False — the
    KV writer itself only re-PUTs when the payload differs.

    Returns {written, unchanged, errors, total}. tvg-ids that have
    no XML mapping are skipped entirely (not counted in any bucket).
    """
    xml = _most_recent_xml()
    if xml is None:
        log.info("publish_nownext: no cached XMLTV; nothing to do")
        return {"written": 0, "unchanged": 0, "errors": 0, "total": 0, "skipped": 0}

    # Step 1: every distinct EPG tvg_id from the cached XMLTV.
    # We use this as the "EPG side" of the matcher.
    try:
        tree = ET.parse(str(xml))
    except Exception as e:
        log.warning("publish_nownext: failed to parse %s: %s", xml, e)
        return {"written": 0, "unchanged": 0, "errors": 0, "total": 0, "skipped": 0}
    epg_tvg_ids = {
        (ch.get("id") or "").strip()
        for ch in tree.getroot().findall("channel")
    }
    epg_tvg_ids.discard("")

    # Step 2: every distinct channel tvg_id, plus the resolution to
    # an EPG tvg_id using the matching strategy described in the
    # module docstring (exact, then @suffix-strip).
    db = await open_db()
    try:
        async with db.execute(
            "SELECT DISTINCT tvg_id FROM channels WHERE tvg_id IS NOT NULL"
        ) as cur:
            channel_tvg_ids = [row["tvg_id"] for row in await cur.fetchall()]
    finally:
        await db.close()

    if not channel_tvg_ids:
        log.info("publish_nownext: no channels with tvg_id; nothing to do")
        return {"written": 0, "unchanged": 0, "errors": 0, "total": 0, "skipped": 0}

    # `mapped` is the set of EPG tvg-ids we need to compute now/next
    # for. `channel_to_epg` maps each M3U tvg_id → EPG tvg_id (or
    # None if no match). The KV key is the M3U tvg_id; the compute
    # key is the EPG tvg_id.
    channel_to_epg: dict[str, Optional[str]] = {}
    mapped: set[str] = set()
    for ctid in channel_tvg_ids:
        if ctid in epg_tvg_ids:
            channel_to_epg[ctid] = ctid
            mapped.add(ctid)
            continue
        stripped = _strip_quality_suffix(ctid)
        if stripped and stripped != ctid and stripped in epg_tvg_ids:
            channel_to_epg[ctid] = stripped
            mapped.add(stripped)
            continue
        channel_to_epg[ctid] = None  # no EPG mapping; skip

    if not mapped:
        log.info(
            "publish_nownext: %d channels w/ tvg_id, 0 matched EPG; nothing to do",
            len(channel_tvg_ids),
        )
        return {"written": 0, "unchanged": 0, "errors": 0, "total": 0, "skipped": 0}

    log.info(
        "publish_nownext: %d/%d channels matched an EPG tvg_id "
        "(%d via exact, %d via @suffix strip)",
        sum(1 for v in channel_to_epg.values() if v is not None),
        len(channel_tvg_ids),
        sum(1 for c, e in channel_to_epg.items() if e and e == c),
        sum(1 for c, e in channel_to_epg.items() if e and e != c),
    )

    # Step 3: compute now/next for the EPG tvg-ids we matched.
    now = datetime.now(timezone.utc)
    computed = compute_nownext(xml, mapped, now)
    generated_at = _format_iso(now)

    # Step 4: write one KV blob per *channel* tvg_id, sourcing the
    # payload from the matching *EPG* tvg_id.
    written = unchanged = errors = 0
    skipped = 0
    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        sem = asyncio.Semaphore(20)

        async def process(channel_tid: str) -> None:
            nonlocal written, unchanged, errors, skipped
            epg_tid = channel_to_epg.get(channel_tid)
            if epg_tid is None:
                return  # no EPG mapping
            payload = computed.get(epg_tid) or _empty()
            async with sem:
                if payload.get("now") is None and payload.get("next") is None:
                    skipped += 1
                body = json.dumps(
                    {
                        "tvg_id": channel_tid,
                        "epg_tvg_id": epg_tid,
                        "generated_at": generated_at,
                        "now": payload["now"],
                        "next": payload["next"],
                    },
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                if not force:
                    from .kv import _get_kv
                    current = await _get_kv(session, f"epg:nownext:{channel_tid}")
                    if current == body:
                        unchanged += 1
                        return
                if await _put_kv(session, f"epg:nownext:{channel_tid}", body):
                    written += 1
                else:
                    errors += 1

        await asyncio.gather(*(process(ctid) for ctid in channel_tvg_ids if channel_to_epg.get(ctid)))

    log.info(
        "KV EPG now/next: written=%d unchanged=%d errors=%d skipped=%d (of %d channels)",
        written, unchanged, errors, skipped, len(channel_tvg_ids),
    )
    return {
        "written": written,
        "unchanged": unchanged,
        "errors": errors,
        "skipped": skipped,
        "total": len(channel_tvg_ids),
    }
