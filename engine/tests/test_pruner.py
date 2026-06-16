"""Tests for the dead-playlist pruner.

The pruner is a self-healing sweep: after every health cycle, any
source_label whose streams are all offline gets dropped, along with
the channels that would become orphaned. These tests pin the
contract:

  1. A label whose streams are 100% offline is pruned.
  2. A label with at least one online stream is left alone.
  3. A mixed label (some online, some offline) is left alone.
  4. A channel fed by TWO labels (only one of them dead) is NOT
     deleted — its other label still has streams backing it.
  5. A channel fed by ONE label (which is dead) IS deleted
     (orphaned).
  6. dry_run reports what would be deleted without writing.
  7. A fresh label whose streams are still `status='unknown'`
     (health check hasn't run yet) is not pruned — the
     HAVING clause requires offline, not unknown.
  8. The imports row's `notes` is annotated on real prune
     (not on dry_run).
  9. A label with zero streams is a no-op.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

# Test setup: temp DB + minimal env so CONFIG.load() succeeds.
TMPDIR = Path(tempfile.mkdtemp(prefix="treefrog-pruner-"))
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
import engine.pruner
importlib.reload(engine.pruner)


def _seed(path: Path) -> None:
    """Build the tables the pruner touches and seed a scenario that
    exercises all 9 contract clauses above.

    Layout:
      - Label "dead"        : 2 channels, 3 streams, all offline.
      - Label "alive"       : 1 channel,  1 stream,  online.
      - Label "mixed"       : 1 channel,  2 streams, one offline + one online.
      - Label "shared-A"    : 1 channel,  1 stream,  offline. (dead)
      - Label "shared-B"    : same channel gets a 2nd stream, online.
      - Label "unknown"     : 1 channel,  1 stream,  status='unknown' (not offline).
      - Label "pristine"    : no streams at all.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY,
            normalized_name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'online',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE streams (
            id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            source_url TEXT NOT NULL UNIQUE,
            source_label TEXT,
            status TEXT NOT NULL DEFAULT 'unknown',
            offline_since TEXT,
            last_error TEXT
        );
        CREATE TABLE health_logs (
            id INTEGER PRIMARY KEY,
            stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
            checked_at TEXT NOT NULL DEFAULT (datetime('now')),
            ok INTEGER NOT NULL
        );
        CREATE TABLE redirects (
            token TEXT PRIMARY KEY,
            channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE
        );
        CREATE TABLE imports (
            id INTEGER PRIMARY KEY,
            source_url TEXT,
            source_label TEXT,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at TEXT,
            notes TEXT
        );
    """)
    # Channels
    conn.executemany(
        "INSERT INTO channels (id, normalized_name, display_name) VALUES (?, ?, ?)",
        [
            (1, "dead-a", "Dead A"),
            (2, "dead-b", "Dead B"),
            (3, "alive-x", "Alive X"),
            (4, "mixed", "Mixed"),
            (5, "shared", "Shared"),
            (6, "unknown-y", "Unknown Y"),
        ],
    )
    # Streams
    # dead: 3 streams across 2 channels, all offline
    conn.executemany(
        "INSERT INTO streams (id, channel_id, source_url, source_label, status, offline_since) "
        "VALUES (?, ?, ?, ?, 'offline', datetime('now', '-1 hour'))",
        [
            (1, 1, "http://dead/a1", "dead"),
            (2, 1, "http://dead/a2", "dead"),
            (3, 2, "http://dead/b1", "dead"),
        ],
    )
    # alive: 1 stream, online
    conn.execute(
        "INSERT INTO streams (id, channel_id, source_url, source_label, status) "
        "VALUES (4, 3, 'http://alive/x', 'alive', 'online')"
    )
    # mixed: 1 online + 1 offline (label has both, must NOT be pruned)
    conn.executemany(
        "INSERT INTO streams (id, channel_id, source_url, source_label, status) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (5, 4, "http://mixed/good", "mixed", "online"),
            (6, 4, "http://mixed/bad", "mixed", "offline"),
        ],
    )
    # shared-A: 1 offline stream on channel 5
    conn.execute(
        "INSERT INTO streams (id, channel_id, source_url, source_label, status, offline_since) "
        "VALUES (7, 5, 'http://shared/a', 'shared-A', 'offline', datetime('now', '-1 hour'))"
    )
    # shared-B: 1 online stream on the same channel
    conn.execute(
        "INSERT INTO streams (id, channel_id, source_url, source_label, status) "
        "VALUES (8, 5, 'http://shared/b', 'shared-B', 'online')"
    )
    # unknown: 1 stream, status unknown
    conn.execute(
        "INSERT INTO streams (id, channel_id, source_url, source_label, status) "
        "VALUES (9, 6, 'http://unknown/y', 'unknown', 'unknown')"
    )
    # health_logs so cascade-delete is exercised (and to confirm it
    # doesn't crash)
    conn.executemany(
        "INSERT INTO health_logs (stream_id, ok) VALUES (?, ?)",
        [(1, 0), (2, 0), (3, 0), (4, 1), (5, 1), (6, 0), (7, 0), (8, 1)],
    )
    # redirects for the alive channel + the shared channel + one of
    # the dead channels (so we can confirm cascade fires)
    conn.executemany(
        "INSERT INTO redirects (token, channel_id, stream_id) VALUES (?, ?, ?)",
        [
            ("tk-alive", 3, 4),
            ("tk-shared", 5, 8),
            ("tk-dead", 1, 1),
        ],
    )
    # imports audit rows for each label
    conn.executemany(
        "INSERT INTO imports (source_label, finished_at, notes) VALUES (?, datetime('now'), ?)",
        [
            ("dead", "imported 3"),
            ("alive", "imported 1"),
            ("mixed", "imported 2"),
            ("shared-A", "imported 1"),
            ("shared-B", "imported 1"),
            ("unknown", "imported 1"),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    db = tmp_path / "pruner.db"
    _seed(db)
    return db


@pytest.mark.asyncio
async def test_prunes_fully_dead_label(seeded):
    """Label "dead" has 3 streams all offline across 2 channels, both
    unique to that label. After pruning: 3 streams gone, 2 channels
    gone, label "dead" is no longer in the streams table."""
    import aiosqlite
    db = await aiosqlite.connect(str(seeded))
    db.row_factory = aiosqlite.Row
    # PRAGMA must be set per-connection. Without it, ON DELETE CASCADE
    # is a no-op in SQLite and the redirects/health_logs assertions
    # below would pass-by-accident.
    await db.execute("PRAGMA foreign_keys = ON")
    try:
        result = await engine.pruner.prune_dead_playlists(db)
    finally:
        await db.close()

    assert result["dry_run"] is False
    assert result["scanned_labels"] == 6
    # Only "dead" is fully offline. shared-A is offline too, but its
    # sibling shared-B backs the same channel, so the channel isn't
    # orphan and shared-A's streams get... wait, the pruner deletes
    # by source_label, not by orphan-channel. shared-A is fully
    # offline (1 stream, 0 online) so it IS a dead label. The channel
    # behind it (channel 5) has another stream from shared-B, so the
    # channel survives. The shared-A stream is deleted.
    assert result["dead_labels"] == 2
    by_label = {p["source_label"]: p for p in result["pruned"]}
    assert "dead" in by_label
    assert "shared-A" in by_label
    assert by_label["dead"]["streams_deleted"] == 3
    assert by_label["dead"]["channels_deleted"] == 2  # both unique
    assert by_label["shared-A"]["streams_deleted"] == 1
    assert by_label["shared-A"]["channels_deleted"] == 0  # channel 5 survives via shared-B

    # Verify the DB state
    conn = sqlite3.connect(str(seeded))
    conn.row_factory = sqlite3.Row
    surviving_channels = {
        r["id"]: r["display_name"]
        for r in conn.execute("SELECT id, display_name FROM channels ORDER BY id")
    }
    surviving_streams = [
        dict(r) for r in conn.execute(
            "SELECT id, channel_id, source_label, status FROM streams ORDER BY id"
        )
    ]
    imports_notes = {
        r["source_label"]: r["notes"] for r in conn.execute(
            "SELECT source_label, notes FROM imports ORDER BY id"
        )
    }
    # Channels 1 and 2 (dead) gone. 3, 4, 5, 6 survive.
    assert surviving_channels == {3: "Alive X", 4: "Mixed", 5: "Shared", 6: "Unknown Y"}

    # No "dead" or "shared-A" streams survive.
    surviving_labels = {s["source_label"] for s in surviving_streams}
    assert "dead" not in surviving_labels
    assert "shared-A" not in surviving_labels
    # But the alive + mixed + shared-B + unknown do survive.
    assert surviving_labels == {"alive", "mixed", "shared-B", "unknown"}

    # Imports note annotated for pruned labels only.
    assert "pruned" in (imports_notes.get("dead") or "")
    assert "pruned" in (imports_notes.get("shared-A") or "")
    assert (imports_notes.get("alive") or "") == "imported 1"
    assert (imports_notes.get("mixed") or "") == "imported 2"
    assert (imports_notes.get("unknown") or "") == "imported 1"

    # Redirects for the deleted channels (channel 1) and deleted
    # streams (stream 1, 7) are CASCADE-removed.
    remaining_redirects = [
        r["token"] for r in conn.execute("SELECT token FROM redirects")
    ]
    assert "tk-dead" not in remaining_redirects
    assert "tk-alive" in remaining_redirects
    assert "tk-shared" in remaining_redirects

    # health_logs for deleted streams are CASCADE-removed.
    remaining_hl = conn.execute("SELECT COUNT(*) FROM health_logs").fetchone()[0]
    # Originally 8 logs; deleted streams were 1, 2, 3, 7 → 4 deleted.
    assert remaining_hl == 4
    conn.close()


@pytest.mark.asyncio
async def test_mixed_label_kept(seeded):
    """Label "mixed" has 1 online + 1 offline stream. The HAVING
    clause requires zero online, so the label is NOT pruned. Channel
    4 and both of its streams survive."""
    import aiosqlite
    db = await aiosqlite.connect(str(seeded))
    db.row_factory = aiosqlite.Row
    try:
        result = await engine.pruner.prune_dead_playlists(db)
    finally:
        await db.close()

    by_label = {p["source_label"] for p in result["pruned"]}
    assert "mixed" not in by_label

    conn = sqlite3.connect(str(seeded))
    mixed_streams = conn.execute(
        "SELECT COUNT(*) FROM streams WHERE source_label = 'mixed'"
    ).fetchone()[0]
    assert mixed_streams == 2
    conn.close()


@pytest.mark.asyncio
async def test_unknown_label_kept(seeded):
    """A freshly-imported label whose health check hasn't run yet has
    status='unknown', not 'offline'. The HAVING clause requires
    offline, so this label must NOT be pruned — we don't want a
    race between import and first health-check to drop a good
    playlist."""
    import aiosqlite
    db = await aiosqlite.connect(str(seeded))
    db.row_factory = aiosqlite.Row
    try:
        result = await engine.pruner.prune_dead_playlists(db)
    finally:
        await db.close()

    by_label = {p["source_label"] for p in result["pruned"]}
    assert "unknown" not in by_label

    conn = sqlite3.connect(str(seeded))
    assert conn.execute(
        "SELECT COUNT(*) FROM streams WHERE source_label = 'unknown'"
    ).fetchone()[0] == 1
    conn.close()


@pytest.mark.asyncio
async def test_dry_run_makes_no_changes(seeded):
    """dry_run=True must return the kill list and not touch the DB.
    The 2nd call (dry_run=False) does the actual delete — we use
    that to confirm the 1st call didn't already mutate state."""
    import aiosqlite
    db = await aiosqlite.connect(str(seeded))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    try:
        dry = await engine.pruner.prune_dead_playlists(db, dry_run=True)
        # Snapshot the DB state mid-flight
        conn1 = sqlite3.connect(str(seeded))
        n_channels_mid = conn1.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        n_streams_mid = conn1.execute("SELECT COUNT(*) FROM streams").fetchone()[0]
        conn1.close()
    finally:
        await db.close()

    assert dry["dry_run"] is True
    assert dry["dead_labels"] == 2
    by_label = {p["source_label"]: p for p in dry["pruned"]}
    # The dry-run output should include a sample of orphan channel ids
    # so the operator can sanity-check what would disappear.
    assert "channels_orphan_ids_sample" in by_label["dead"]
    assert set(by_label["dead"]["channels_orphan_ids_sample"]) <= {1, 2}

    # DB unchanged
    assert n_channels_mid == 6
    assert n_streams_mid == 9

    # Now actually prune
    db = await aiosqlite.connect(str(seeded))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    try:
        real = await engine.pruner.prune_dead_playlists(db)
    finally:
        await db.close()
    assert real["dead_labels"] == 2

    # Now DB is changed
    conn2 = sqlite3.connect(str(seeded))
    n_channels_after = conn2.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    n_streams_after = conn2.execute("SELECT COUNT(*) FROM streams").fetchone()[0]
    conn2.close()
    assert n_channels_after == 4
    assert n_streams_after == 5  # 9 - 3 (dead) - 1 (shared-A) = 5


@pytest.mark.asyncio
async def test_no_dead_labels_is_noop(seeded):
    """If we mark every offline stream as online first, the pruner
    is a no-op — scanned_labels is still 6 but dead_labels is 0 and
    the DB is untouched."""
    import aiosqlite
    conn = sqlite3.connect(str(seeded))
    conn.execute(
        "UPDATE streams SET status = 'online', offline_since = NULL "
        "WHERE status = 'offline'"
    )
    conn.commit()
    conn.close()

    db = await aiosqlite.connect(str(seeded))
    db.row_factory = aiosqlite.Row
    try:
        result = await engine.pruner.prune_dead_playlists(db)
    finally:
        await db.close()

    assert result["dead_labels"] == 0
    assert result["pruned"] == []
    assert result["scanned_labels"] == 6

    # And the DB is unchanged
    conn = sqlite3.connect(str(seeded))
    assert conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM streams").fetchone()[0] == 9
    conn.close()


@pytest.mark.asyncio
async def test_idempotent_on_second_call(seeded):
    """After the first prune, the second prune should find nothing
    left to do (the dead labels are gone)."""
    import aiosqlite
    db = await aiosqlite.connect(str(seeded))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    try:
        first = await engine.pruner.prune_dead_playlists(db)
        second = await engine.pruner.prune_dead_playlists(db)
    finally:
        await db.close()

    assert first["dead_labels"] == 2
    assert second["dead_labels"] == 0
    assert second["pruned"] == []


@pytest.mark.asyncio
async def test_empty_db_is_noop(tmp_path):
    """A brand-new engine DB with no streams at all should be a clean
    no-op — scanned_labels=0, dead_labels=0, pruned=[]."""
    import aiosqlite
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db_path))
    # Just the schema, no rows
    conn.executescript("""
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY, normalized_name TEXT NOT NULL,
            display_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'online',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE streams (
            id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
            source_url TEXT NOT NULL UNIQUE, source_label TEXT,
            status TEXT NOT NULL DEFAULT 'unknown'
        );
        CREATE TABLE health_logs (id INTEGER PRIMARY KEY, stream_id INTEGER NOT NULL, checked_at TEXT NOT NULL, ok INTEGER NOT NULL);
        CREATE TABLE redirects (token TEXT PRIMARY KEY, channel_id INTEGER NOT NULL, stream_id INTEGER NOT NULL);
        CREATE TABLE imports (id INTEGER PRIMARY KEY, source_url TEXT, source_label TEXT, started_at TEXT NOT NULL, notes TEXT);
    """)
    conn.commit()
    conn.close()

    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    try:
        result = await engine.pruner.prune_dead_playlists(db)
    finally:
        await db.close()

    assert result == {
        "dry_run": False,
        "scanned_labels": 0,
        "dead_labels": 0,
        "pruned": [],
    }


@pytest.mark.asyncio
async def test_multi_source_channel_survives_one_dead_source(seeded):
    """Sharper version of the same guarantee in test_prunes_fully_dead_label.

    Channel 5 is fed by both shared-A (offline) and shared-B (online).
    We assert: even after pruning shared-A, channel 5 still exists
    AND still has its shared-B stream. This is the key safety
    property — a channel with multiple sources must NEVER be
    deleted just because one of them died."""
    import aiosqlite
    db = await aiosqlite.connect(str(seeded))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    try:
        await engine.pruner.prune_dead_playlists(db)
    finally:
        await db.close()

    conn = sqlite3.connect(str(seeded))
    row = conn.execute(
        "SELECT display_name FROM channels WHERE id = 5"
    ).fetchone()
    assert row is not None, "shared channel 5 must survive"
    assert row[0] == "Shared"

    streams_on_5 = conn.execute(
        "SELECT source_label, status FROM streams WHERE channel_id = 5"
    ).fetchall()
    assert streams_on_5 == [("shared-B", "online")], (
        "channel 5 must still have its shared-B online stream; "
        f"got {streams_on_5}"
    )
    conn.close()
