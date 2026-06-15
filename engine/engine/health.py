"""Two-stage stream health check + 30-minute scheduler.

Stage 1: HEAD the manifest URL with a 5s timeout. Status 2xx/3xx passes.
Stage 2: GET the first 16 KB and verify the body contains "#EXTM3U" AND
         at least one "#EXTINF". This catches the common case where the
         server returns 200 to HEAD/GET but the playlist is empty or
         auth-failed.

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
    headers = {
        "User-Agent": "TreeFrog-Engine/0.1 (+health-check)",
        "Accept": "*/*",
    }
    started = time.monotonic()
    try:
        # ---- Stage 1: cheap HEAD probe ----
        async with session.head(
            url, timeout=timeout, headers=headers, allow_redirects=True
        ) as head:
            if head.status >= 400:
                return ProbeResult(
                    stream_id, False, int((time.monotonic() - started) * 1000),
                    f"head status {head.status}",
                )
            # Some servers don't support HEAD on .m3u8 — fall through to GET.

        # ---- Stage 2: GET first 16 KB, sniff for #EXTM3U + #EXTINF ----
        async with session.get(
            url, timeout=timeout, headers=headers, allow_redirects=True
        ) as resp:
            if resp.status >= 400:
                return ProbeResult(
                    stream_id, False, int((time.monotonic() - started) * 1000),
                    f"get status {resp.status}",
                )
            # Read up to HEALTH_MANIFEST_BYTES; bail early if smaller.
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(4096):
                buf.extend(chunk)
                if len(buf) >= CONFIG.health_manifest_bytes:
                    break
                # If we've already seen #EXTM3U + at least one #EXTINF, stop.
                if b"#EXTM3U" in buf and buf.count(b"#EXTINF") >= 1:
                    break

        body = bytes(buf)
        if b"#EXTM3U" not in body:
            return ProbeResult(
                stream_id, False, int((time.monotonic() - started) * 1000),
                "missing #EXTM3U in manifest",
            )
        if body.count(b"#EXTINF") < 1:
            return ProbeResult(
                stream_id, False, int((time.monotonic() - started) * 1000),
                "no #EXTINF entries in manifest",
            )

        return ProbeResult(
            stream_id, True, int((time.monotonic() - started) * 1000), None
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
                        last_latency_ms = ?
                    WHERE id = ?
                    """,
                    (now, now, r.latency_ms, stream_id),
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
