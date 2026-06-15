"""Dataclasses for channel/stream/etc. Used at the parser/DB boundary.

These are intentionally small — the DB stores everything, and we just lift
rows into these types for code that doesn't want to deal with aiosqlite.Row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class M3UEntry:
    """A single entry parsed from an M3U file, before consolidation."""
    name: str
    url: str
    tvg_id: Optional[str] = None
    tvg_name: Optional[str] = None
    tvg_logo: Optional[str] = None
    group_title: str = "Other"


@dataclass(slots=True)
class Channel:
    id: int
    normalized_name: str
    display_name: str
    tvg_id: Optional[str]
    group_title: str
    logo_url: Optional[str]
    bouquet: str
    status: str
    availability_pct: float


@dataclass(slots=True)
class Stream:
    id: int
    channel_id: int
    source_url: str
    source_label: Optional[str]
    priority: int
    status: str
    last_ok_at: Optional[str]
    offline_since: Optional[str]
    last_latency_ms: Optional[int]


@dataclass(slots=True)
class HealthResult:
    stream_id: int
    ok: bool
    latency_ms: Optional[int]
    error: Optional[str]
    checked_at: str = field(default_factory=lambda: _now())


def _now() -> str:
    """ISO-ish UTC timestamp, second precision. SQLite-friendly."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
