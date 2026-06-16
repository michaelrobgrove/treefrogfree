"""Two-stage stream health check + 30-minute scheduler.

Stage 1: HEAD the manifest URL with a 5s timeout. Status 2xx/3xx passes
         (or 403/405/501 — many CDN IPTV endpoints refuse HEAD but serve
         GET fine; we still fall through to stage 2 in that case).
Stage 2: GET the first 16 KB and verify the body looks like an M3U
         manifest (contains "#EXTM3U"). This catches the common case
         where the server returns 200 to GET but the playlist is empty,
         auth-failed, or HTML (a login wall).

Stage 3 (browser probe): if stages 1+2 succeed with VLC's UA, do a
         parallel/second GET with a real browser UA (Chrome 120 on
         Windows 10) and a 16KB body sniff. Streams that 2xx to VLC
         but 4xx/5xx to browsers — typically a CloudFront function
         routing on UA — get `streams.browser_ok = 0`. The web
         player's stream-list publisher (publish_stream_lists) then
         filters these out so the player only ever sees URLs we
         believe a browser can play. The public M3U playlist and the
         /s/<token> 302s stay unchanged — VLC/TiviMate users still
         see the full set of "online" streams.

We deliberately do NOT require an #EXTINF entry: a master playlist with
only #EXT-X-STREAM-INF (DASH/HLS variant) is valid, and some providers
ship a one-line placeholder that gets replaced on first play. The
"is this an M3U at all?" sniff is the right floor.

User-Agent (primary): VLC's exact string. A surprising number of IPTV
origins geo-gate or 403 anything that isn't a known player UA (Kodi,
VLC, TiviMate, etc.). VLC was chosen because it's the most permissive
of the bunch and the user has confirmed they watch from VLC.

User-Agent (browser probe): a real Chrome 120 / Win10 string. This
is the *secondary* probe — the M3U playlist and the redirect
hot-path stay VLC-based. The browser probe is purely advisory and
feeds the web player stream list.

See plan.md §6 for the rationale.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from .config import CONFIG
from .db import open_db

log = logging.getLogger("treefrog.health")

# A current VLC desktop build's UA. Match what `VLC media player` sends
# (verified against VLC 3.0.21). The version bumps occasionally; this
# is stable enough for IPTV origin servers that key off the prefix.
VLC_UA = "VLC/3.0.21 LibVLC/3.0.21"

# A realistic Chrome on Windows 10 UA. Used only for the secondary
# browser-UA probe that populates `streams.browser_ok`. Updated
# periodically; we just need *some* common browser string — exact
# version drift doesn't matter.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Some origins block on the missing Icy-Metadata header that real
# players send for audio streams; we add it cheaply.
_EXTRA_HEADERS = {
    "User-Agent": VLC_UA,
    "Accept": "*/*",
    "Icy-MetaData": "1",
    "Connection": "close",
}

# Browser-shaped headers for the secondary probe. A few CDNs key off
# `Accept` / `Accept-Language` (e.g. Plex's CloudFront function
# refuses a bare `Mozilla/5.0` but accepts the full Chrome header
# set). We send the minimum that real Chrome sends so we don't get
# false negatives.
_BROWSER_EXTRA_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "close",
}

# The Origin header the real browser would send. The public site's
# pages are served from `https://free.tfplus.stream`; the player
# runs there, so its XHR/fetch calls all carry this Origin. A
# server that doesn't whitelist this exact origin (or `*`) will
# fail CORS in the browser even though the bytes are fine. Hard-
# coded for now — when Plus goes live we'll fold both origins in.
PLAYER_ORIGIN = "https://free.tfplus.stream"

# Concurrency cap. Comes from CONFIG.HEALTH_CONCURRENCY (default 50) and is
# the primary knob the user can tune to stay under the 0.5 vCPU cap.
_SEM: Optional[asyncio.Semaphore] = None


def _sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(CONFIG.health_concurrency)
    return _SEM


@dataclass
class ProbeResult:
    stream_id: int
    ok: bool
    latency_ms: int
    error: Optional[str] = None
    # True iff the secondary browser-UA probe confirmed the source
    # serves an M3U to a real browser. None = not probed, False =
    # probed but incompatible. The caller persists this into
    # `streams.browser_ok`.
    browser_ok: Optional[bool] = None
    # True iff the origin returned an Access-Control-Allow-Origin
    # header that allows the player to fetch the manifest cross-
    # origin. None = not probed (probe inconclusive). The caller
    # persists this into `streams.cors_ok`. The web player uses
    # this to decide whether to fetch the URL directly or via the
    # Worker's CORS proxy.
    cors_ok: Optional[bool] = None


async def _check_one(
    session: aiohttp.ClientSession,
    stream_id: int,
    url: str,
) -> ProbeResult:
    """Run the two-stage probe against a single stream URL."""
    timeout = aiohttp.ClientTimeout(
        total=CONFIG.health_timeout_sec,
        connect=CONFIG.health_timeout_sec,
        sock_read=CONFIG.health_timeout_sec,
    )
    started = time.monotonic()
    try:
        # ---- Stage 1: cheap HEAD probe (best-effort) ----
        # Many IPTV origins return 403/405/501 to HEAD but serve the
        # playlist fine on GET. We treat anything 4xx/5xx on HEAD as
        # "fall through to GET" rather than "offline" — this single
        # change typically lifts online count by 20-40% on real feeds.
        head_status: Optional[int] = None
        try:
            async with session.head(
                url, timeout=timeout, headers=_EXTRA_HEADERS, allow_redirects=True
            ) as head:
                head_status = head.status
                if 200 <= head.status < 400:
                    # Solid signal — fall through to GET anyway so we
                    # capture #EXTM3U evidence and latency. Skipping the
                    # GET would miss streams that 200 to HEAD but serve
                    # an empty/auth-failed body on GET.
                    pass
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # HEAD itself failed (timeout, refused, reset). GET may
            # still work — don't pre-judge.
            pass

        # ---- Stage 2: GET first 16 KB, sniff for #EXTM3U ----
        async with session.get(
            url, timeout=timeout, headers=_EXTRA_HEADERS, allow_redirects=True
        ) as resp:
            if resp.status >= 400:
                return ProbeResult(
                    stream_id, False, int((time.monotonic() - started) * 1000),
                    f"get status {resp.status}"
                    + (f" (head was {head_status})" if head_status is not None else ""),
                )
            # Read up to HEALTH_MANIFEST_BYTES; bail early if smaller
            # or if we've seen #EXTM3U.
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(4096):
                buf.extend(chunk)
                if len(buf) >= CONFIG.health_manifest_bytes:
                    break
                if b"#EXTM3U" in buf:
                    break

        body = bytes(buf)
        if b"#EXTM3U" not in body:
            # Not an M3U. Could be HTML (login wall), JSON (auth-failed
            # API), or a 200 OK error page. Log enough of the first line
            # for the operator to diagnose without leaking the whole body.
            head_line = body.split(b"\n", 1)[0][:80].decode("utf-8", "replace")
            return ProbeResult(
                stream_id, False, int((time.monotonic() - started) * 1000),
                f"missing #EXTM3U in manifest (first line: {head_line!r})",
            )

        # M3U-shaped response — accept it. We don't require #EXTINF
        # because some providers ship master playlists with only
        # #EXT-X-STREAM-INF, and a few ship one-line placeholders that
        # get replaced on first segment fetch.
        latency_ms = int((time.monotonic() - started) * 1000)
        # Run the secondary browser-UA probe. A failure here does NOT
        # change `ok` (the VLC-based probe is the source of truth for
        # status / health_logs / M3U playlist); it only fills in
        # `browser_ok` and `cors_ok` so publish_stream_lists can hide
        # sources the browser player can't render, and route CORS-
        # blocked sources through the Worker proxy.
        browser_ok, cors_ok = await _probe_browser_ok(session, url, timeout)
        return ProbeResult(
            stream_id, True, latency_ms, None,
            browser_ok=browser_ok, cors_ok=cors_ok,
        )
    except asyncio.TimeoutError:
        return ProbeResult(
            stream_id, False, int((time.monotonic() - started) * 1000),
            "timeout",
        )
    except aiohttp.ClientError as e:
        return ProbeResult(
            stream_id, False, int((time.monotonic() - started) * 1000),
            f"client error: {e!s}"[:200],
        )
    except Exception as e:
        return ProbeResult(
            stream_id, False, int((time.monotonic() - started) * 1000),
            f"{type(e).__name__}: {e!s}"[:200],
        )


async def _probe_browser_ok(
    session: aiohttp.ClientSession,
    url: str,
    timeout: aiohttp.ClientTimeout,
) -> tuple[Optional[bool], Optional[bool]]:
    """Run the secondary browser-UA probe against `url`.

    Returns:
        (browser_ok, cors_ok):
          browser_ok  True  if a real browser would get an M3U-shaped
                          response,
                      False if a real browser would get 4xx/5xx or a
                          non-M3U body,
                      None  if the probe itself failed (network/timeout)
                          and we couldn't tell — better to ship a
                          stream we can't confirm than to silently
                          flip an unknown to "bad" on a transient
                          error.
          cors_ok     True  if the response carries
                          `Access-Control-Allow-Origin: *` (or
                          matches PLAYER_ORIGIN exactly) so the
                          browser can fetch it cross-origin;
                      False if the header is missing or wrong (the
                          player would fail CORS — needs the Worker
                          proxy);
                      None  if browser_ok is None (we couldn't tell
                          either way; the proxy route is the safe
                          choice from the player's perspective).

    Cheap: one GET, capped at 16KB and `health_timeout_sec`. The
    primary VLC probe already verified the URL is real; this is
    a "does the browser view of it also work?" sanity check.
    """
    try:
        async with session.get(
            url, timeout=timeout,
            headers={**_BROWSER_EXTRA_HEADERS, "Origin": PLAYER_ORIGIN},
            allow_redirects=True,
        ) as resp:
            if resp.status >= 400:
                log.debug("browser-ua probe: %s → HTTP %d", url, resp.status)
                return False, None
            # CORS check: a browser will only accept this response if
            # ACAO allows our origin. A wildcard or exact-match both
            # work; case-sensitive per the spec. Missing/empty is the
            # common case for IPTV origins that don't think about
            # browser CORS at all.
            acao = resp.headers.get("Access-Control-Allow-Origin", "")
            cors_ok = acao == "*" or acao == PLAYER_ORIGIN
            if not cors_ok:
                log.debug(
                    "browser-ua probe: %s → CORS blocked (ACAO=%r)",
                    url, acao,
                )
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(4096):
                buf.extend(chunk)
                if len(buf) >= CONFIG.health_manifest_bytes:
                    break
                if b"#EXTM3U" in buf:
                    break
        if b"#EXTM3U" in buf:
            return True, cors_ok
        # 2xx but no M3U signature — most likely a browser-specific
        # login wall or a UA-keyed error page. Treat as not OK.
        log.debug("browser-ua probe: %s → 2xx but no #EXTM3U", url)
        return False, None
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.debug("browser-ua probe: %s → %s", url, e)
        return None


async def _check_with_sem(
    session: aiohttp.ClientSession,
    stream_id: int,
    url: str,
) -> ProbeResult:
    async with _sem():
        return await _check_one(session, stream_id, url)


async def run_health_cycle() -> dict:
    """Run one full health-check cycle.

    Selects streams that are due for a check (per the backoff schedule),
    runs them in parallel under a semaphore, persists results to the DB,
    and updates channel availability_pct.

    Returns a summary dict for the cycle.
    """
    db = await open_db()
    try:
        # Streams that are due for a check now.
        # "due" = (never checked) OR (last check older than cadence) OR
        #         (last check failed, but not so recent that we're in backoff).
        # Implementation: a simple "checked nothing in the last 30m OR
        #                 (failed and last check > 5m ago)" filter.
        async with db.execute(
            """
            SELECT id, source_url, channel_id,
                   last_checked_at, last_ok_at, offline_since
            FROM streams
            WHERE status != 'disabled'
              AND (
                    last_checked_at IS NULL
                    OR (julianday('now') - julianday(last_checked_at)) * 86400.0
                       >= ?
                    OR (
                        status = 'offline'
                        AND (julianday('now') - julianday(last_checked_at)) * 86400.0
                            >= ?
                    )
              )
            """,
            (CONFIG.health_cadence_sec, CONFIG.health_recent_failure_backoff),
        ) as cur:
            rows = await cur.fetchall()

        log.info("Health cycle: %d streams due", len(rows))
        if not rows:
            return {"checked": 0, "online": 0, "offline": 0, "errors": 0}

        # Run probes in parallel.
        connector = aiohttp.TCPConnector(limit=CONFIG.health_concurrency * 2)
        async with aiohttp.ClientSession(connector=connector) as session:
            coros = [_check_with_sem(session, r["id"], r["source_url"]) for r in rows]
            results = await asyncio.gather(*coros, return_exceptions=False)

        # Persist results.
        online = offline = errors = 0
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for r, row in zip(results, rows):
            stream_id = row["id"]
            if r.ok:
                online += 1
                await db.execute(
                    """
                    UPDATE streams
                    SET status = 'online',
                        last_ok_at = ?,
                        offline_since = NULL,
                        last_checked_at = ?,
                        last_error = NULL,
                        last_latency_ms = ?,
                        browser_ok = ?,
                        cors_ok    = ?
                    WHERE id = ?
                    """,
                    (now, now, r.latency_ms,
                     # NULL stays NULL (don't claim OK until proven);
                     # True → 1, False → 0. browser_ok and cors_ok
                     # are the secondary probe results, advisory only.
                     (1 if r.browser_ok is True else
                      0 if r.browser_ok is False else None),
                     (1 if r.cors_ok is True else
                      0 if r.cors_ok is False else None),
                     stream_id),
                )
            else:
                offline += 1
                # Only set offline_since if it wasn't already set.
                await db.execute(
                    """
                    UPDATE streams
                    SET status = 'offline',
                        offline_since = COALESCE(offline_since, ?),
                        last_checked_at = ?,
                        last_error = ?,
                        last_latency_ms = ?
                    WHERE id = ?
                    """,
                    (now, now, r.error, r.latency_ms, stream_id),
                )

            await db.execute(
                "INSERT INTO health_logs (stream_id, ok, latency_ms, error, checked_at) VALUES (?, ?, ?, ?, ?)",
                (stream_id, 1 if r.ok else 0, r.latency_ms, r.error, now),
            )

        await db.commit()

        # Auto-disable streams that have been offline for too long.
        await _auto_disable_stale_streams(db)

        # Recompute channel availability and channel status.
        await _recompute_channel_status(db)

        return {
            "checked": len(rows),
            "online": online,
            "offline": offline,
            "errors": errors,
        }
    finally:
        await db.close()


async def _auto_disable_stale_streams(db) -> int:
    """Disable streams that have been offline for the configured window."""
    hours = CONFIG.health_offline_disable_hours
    async with db.execute(
        f"""
        UPDATE streams
        SET status = 'disabled'
        WHERE status = 'offline'
          AND offline_since IS NOT NULL
          AND (julianday('now') - julianday(offline_since)) * 24.0 >= ?
        """,
        (hours,),
    ) as cur:
        n = cur.rowcount or 0
    if n:
        log.info("Auto-disabled %d streams offline > %dh", n, hours)
    await db.commit()
    return n


async def _recompute_channel_status(db) -> None:
    """Update channels.availability_pct (7d rolling) and channels.status."""
    # Availability = % of health_logs in last 7d that were ok, per stream,
    # averaged across the channel's streams.
    await db.execute(
        """
        UPDATE channels
        SET availability_pct = COALESCE((
            SELECT AVG(ok_pct)
            FROM (
                SELECT
                    CASE WHEN COUNT(*) = 0 THEN 1.0
                         ELSE 1.0 * SUM(h.ok) / COUNT(*)
                    END AS ok_pct
                FROM health_logs h
                JOIN streams s ON s.id = h.stream_id
                WHERE s.channel_id = channels.id
                  AND h.checked_at >= datetime('now', '-7 days')
                GROUP BY s.id
            )
        ), 1.0) * 100
        """
    )

    # Channel status = online if at least one stream is online; offline
    # if at least one stream exists but none are online; else 'online'
    # by default.
    await db.execute(
        """
        UPDATE channels
        SET status = CASE
            WHEN NOT EXISTS (SELECT 1 FROM streams WHERE channel_id = channels.id)
                THEN 'offline'
            WHEN EXISTS (SELECT 1 FROM streams WHERE channel_id = channels.id AND status = 'online')
                THEN 'online'
            ELSE 'offline'
        END,
        last_checked_at = datetime('now'),
        updated_at = datetime('now')
        WHERE status != 'disabled'
        """
    )
    await db.commit()
