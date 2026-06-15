"""Tests for the streams_kv publisher.

The publisher writes a `streams:<token>` JSON blob to Cloudflare KV
per online channel. The HLS web player reads that blob to get the
ordered failover list. We test the publish step with a fake
aiohttp session that records the PUTs/GETs/DELETEs without actually
hitting Cloudflare.

These tests do NOT mock Cloudflare's API surface — they patch
`aiohttp.ClientSession` at the module level so the publisher's
calls land in an in-memory store. That's enough to verify:
  - the JSON payload shape (channel_id, tvg_id, name, logo, urls)
  - ordering of urls (priority ASC, id ASC)
  - deletion of `streams:<token>` when the channel has no online
    streams
  - diff-and-skip (second call writes nothing when nothing changed)
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Test setup: temp DB + minimal env so CONFIG.load() succeeds.
TMPDIR = Path(tempfile.mkdtemp(prefix="treefrog-streams-kv-"))
import os
os.environ["DATA_DIR"] = str(TMPDIR)
os.environ["LOG_DIR"] = str(TMPDIR / "logs")
os.environ["DB_PATH"] = str(TMPDIR / "treefrog.db")
os.environ["CF_API_TOKEN"] = "fake"
os.environ["CF_ACCOUNT_ID"] = "fake"
os.environ["CF_KV_NAMESPACE_ID"] = "fake"
os.environ["ADMIN_TOKEN"] = "test-token"

import importlib
import engine.config
importlib.reload(engine.config)
import engine.db
importlib.reload(engine.db)
import engine.publisher.streams_kv
importlib.reload(engine.publisher.streams_kv)
import engine.publisher.kv  # for the _put_kv / _get_kv / _delete_kv helpers


class _FakeKVStore:
    """In-memory KV stand-in. Records every operation so tests
    can assert on writes, deletes, and the JSON payloads."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.puts: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        self.gets: list[str] = []
        # If True, _put_kv / _delete_kv return False (simulate API error).
        self.fail_writes = False
        self.fail_deletes = False

    def reset(self) -> None:
        self.puts.clear()
        self.deletes.clear()
        self.gets.clear()


# Module-level so the publisher (imported at the top) sees the
# same instance across calls.
_KV = _FakeKVStore()


class _FakeResponse:
    """Mimics aiohttp.ClientResponse just enough for the publisher's
    await resp.text() / resp.status / resp.read() patterns."""

    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body.decode("utf-8")

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *a) -> None:
        pass


class _FakeSession:
    """Drop-in for aiohttp.ClientSession. Intercepts the URL paths
    the publisher hits and routes them to the in-memory _KV store."""

    def __init__(self, *a, **kw) -> None:
        pass

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *a) -> None:
        pass

    def _route(self, method: str, url: str) -> _FakeResponse:
        # Cloudflare KV API URLs look like:
        #   https://api.cloudflare.com/.../values/<key>
        # Extract the trailing key.
        if "/values/" not in url:
            return _FakeResponse(404)
        key = url.rsplit("/values/", 1)[1]
        if method == "GET":
            _KV.gets.append(key)
            if key in _KV.store:
                return _FakeResponse(200, _KV.store[key].encode("utf-8"))
            return _FakeResponse(404)
        if method == "PUT":
            return _FakeResponse(200)  # body set by caller via the request() wrapper
        if method == "DELETE":
            return _FakeResponse(200)
        return _FakeResponse(405)

    def get(self, url: str, **kw) -> _FakeResponse:
        return self._route("GET", url)

    def put(self, url: str, *, headers: dict = None, data: Any = None, **kw) -> _FakeResponse:
        if "/values/" in url:
            key = url.rsplit("/values/", 1)[1]
            if _KV.fail_writes:
                return _FakeResponse(500)
            body = data() if callable(data) else data
            if isinstance(body, str):
                body = body.encode("utf-8")
            _KV.store[key] = body.decode("utf-8")
            _KV.puts.append((key, _KV.store[key]))
            return _FakeResponse(200)
        return _FakeResponse(404)

    def delete(self, url: str, **kw) -> _FakeResponse:
        if "/values/" in url:
            key = url.rsplit("/values/", 1)[1]
            if _KV.fail_deletes:
                return _FakeResponse(500)
            existed = key in _KV.store
            _KV.store.pop(key, None)
            _KV.deletes.append(key)
            # Mirror the real _delete_kv behavior: only count as
            # success if the key actually existed.
            return _FakeResponse(200) if existed else _FakeResponse(404)
        return _FakeResponse(404)


def _seed_db(path: Path) -> None:
    """Create the tables the streams_kv publisher reads from and
    seed three channels: 1 with one online stream, 1 with three
    online streams (different priorities), 1 with zero online."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY,
            normalized_name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            tvg_id TEXT,
            tvg_name TEXT,
            group_title TEXT NOT NULL DEFAULT 'Other',
            logo_url TEXT,
            bouquet TEXT NOT NULL DEFAULT 'Auto',
            status TEXT NOT NULL DEFAULT 'online',
            availability_pct REAL NOT NULL DEFAULT 100.0,
            last_checked_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE streams (
            id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            source_url TEXT NOT NULL,
            source_label TEXT,
            priority INTEGER NOT NULL DEFAULT 100,
            status TEXT NOT NULL DEFAULT 'unknown',
            last_ok_at TEXT,
            offline_since TEXT,
            last_checked_at TEXT,
            last_error TEXT,
            last_latency_ms INTEGER,
            UNIQUE (source_url)
        );
        CREATE TABLE redirects (
            token TEXT PRIMARY KEY,
            channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    # Three channels
    conn.execute(
        "INSERT INTO channels (id, normalized_name, display_name, tvg_id, status) "
        "VALUES (1, 'pbs kids', 'PBS Kids', 'pbskids.us', 'online')"
    )
    conn.execute(
        "INSERT INTO channels (id, normalized_name, display_name, tvg_id, status) "
        "VALUES (2, 'cnn', 'CNN', 'cnn.us', 'online')"
    )
    conn.execute(
        "INSERT INTO channels (id, normalized_name, display_name, tvg_id, status) "
        "VALUES (3, 'old net', 'Old Net', 'oldnet.us', 'offline')"
    )
    # PBS Kids: one online stream
    conn.execute(
        "INSERT INTO streams (channel_id, source_url, status, priority) "
        "VALUES (1, 'http://provider-a/pbs.m3u8', 'online', 100)"
    )
    # CNN: three online streams at different priorities
    conn.execute(
        "INSERT INTO streams (channel_id, source_url, status, priority) "
        "VALUES (2, 'http://provider-c/cnn.m3u8', 'online', 50)"   # highest priority
    )
    conn.execute(
        "INSERT INTO streams (channel_id, source_url, status, priority) "
        "VALUES (2, 'http://provider-a/cnn.m3u8', 'online', 100)"
    )
    conn.execute(
        "INSERT INTO streams (channel_id, source_url, status, priority) "
        "VALUES (2, 'http://provider-b/cnn.m3u8', 'online', 100)"  # same prio as A, lower id → first
    )
    # Old Net: one offline stream
    conn.execute(
        "INSERT INTO streams (channel_id, source_url, status, priority) "
        "VALUES (3, 'http://provider-z/old.m3u8', 'offline', 100)"
    )
    # Redirects for the two online channels
    conn.execute(
        "INSERT INTO redirects (token, channel_id, stream_id) VALUES ('aaaaaa', 1, 1)"
    )
    # stream_id 2 is the highest-priority CNN stream (provider-c)
    conn.execute(
        "INSERT INTO redirects (token, channel_id, stream_id) VALUES ('bbbbbb', 2, 2)"
    )
    # No redirect for Old Net — it has no online stream so the
    # publisher won't see it.
    conn.commit()
    conn.close()


@pytest.fixture
def seeded_db(monkeypatch, tmp_path) -> Path:
    """Stand up a fresh DB and return its path."""
    db = tmp_path / "test.db"
    _seed_db(db)
    return db


@pytest.fixture(autouse=True)
def _patch_session_and_db(monkeypatch):
    """Every test gets a clean fake KV store and a fresh
    open_db() that points at the per-test DB."""
    _KV.store.clear()
    _KV.puts.clear()
    _KV.deletes.clear()
    _KV.gets.clear()
    _KV.fail_writes = False
    _KV.fail_deletes = False
    # Patch ClientSession inside the publisher's namespace.
    import aiohttp
    monkeypatch.setattr(
        engine.publisher.streams_kv.aiohttp,
        "ClientSession",
        _FakeSession,
    )
    yield


@pytest.mark.asyncio
async def test_writes_streams_kv_for_each_online_channel(seeded_db):
    """Two redirects (PBS Kids + CNN). Both should land in KV with
    the right JSON shape and the failover URLs in priority order."""
    async def _open_my_db():
        import aiosqlite
        c = await aiosqlite.connect(str(seeded_db))
        c.row_factory = aiosqlite.Row
        return c
    # Patch open_db inside the publisher's namespace so it opens
    # *our* test DB.
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            engine.publisher.streams_kv, "open_db", _open_my_db
        )
        result = await engine.publisher.streams_kv.publish_stream_lists()
    finally:
        monkeypatch.undo()

    assert result["total"] == 2
    assert result["written"] == 2
    assert result["errors"] == 0

    # PBS Kids — one URL
    pbs = json.loads(_KV.store["streams:aaaaaa"])
    assert pbs["channel_id"] == 1
    assert pbs["tvg_id"] == "pbskids.us"
    assert pbs["name"] == "PBS Kids"
    assert pbs["urls"] == ["http://provider-a/pbs.m3u8"]

    # CNN — three URLs, ordered priority ASC then id ASC.
    # The seeding above has provider-c at prio 50, then
    # provider-a and provider-b at prio 100 (same prio, a's id
    # is lower so it comes first).
    cnn = json.loads(_KV.store["streams:bbbbbb"])
    assert cnn["channel_id"] == 2
    assert cnn["urls"] == [
        "http://provider-c/cnn.m3u8",
        "http://provider-a/cnn.m3u8",
        "http://provider-b/cnn.m3u8",
    ]


@pytest.mark.asyncio
async def test_deletes_streams_kv_when_channel_offline(seeded_db):
    """If a channel had a streams:<token> key but its only stream
    went offline, the publisher must delete the key so the player
    gets a 404 rather than an empty urls list."""
    # Pre-seed KV with a streams:<token> entry for one of the
    # channels, then take that channel offline.
    _KV.store["streams:aaaaaa"] = json.dumps({
        "channel_id": 1, "name": "PBS Kids", "tvg_id": "pbskids.us",
        "logo": None, "urls": ["http://provider-a/pbs.m3u8"],
    })
    conn = sqlite3.connect(str(seeded_db))
    conn.execute("UPDATE channels SET status = 'offline' WHERE id = 1")
    conn.execute("UPDATE streams  SET status = 'offline' WHERE channel_id = 1")
    conn.commit()
    conn.close()

    async def _open_my_db():
        import aiosqlite
        c = await aiosqlite.connect(str(seeded_db))
        c.row_factory = aiosqlite.Row
        return c
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            engine.publisher.streams_kv, "open_db", _open_my_db
        )
        result = await engine.publisher.streams_kv.publish_stream_lists()
    finally:
        monkeypatch.undo()

    assert result["deleted"] >= 1
    assert "streams:aaaaaa" not in _KV.store


@pytest.mark.asyncio
async def test_diff_and_skip_on_second_call(seeded_db):
    """The second call (force=False) must not rewrite the same
    value back to KV. It should report unchanged=N, written=0."""
    async def _open_my_db():
        import aiosqlite
        c = await aiosqlite.connect(str(seeded_db))
        c.row_factory = aiosqlite.Row
        return c
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            engine.publisher.streams_kv, "open_db", _open_my_db
        )
        first = await engine.publisher.streams_kv.publish_stream_lists()
        puts_after_first = len(_KV.puts)
        # Second call without force.
        second = await engine.publisher.streams_kv.publish_stream_lists()
    finally:
        monkeypatch.undo()

    assert first["written"] == 2
    assert second["written"] == 0
    assert second["unchanged"] == 2
    # No new PUTs on the second call.
    assert len(_KV.puts) == puts_after_first
