"""SQLite connection + migrations.

Single-writer engine process → no contention. WAL gives us concurrent reads
from the admin API without blocking the scheduler. See plan.md §4.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from .config import CONFIG

log = logging.getLogger("treefrog.db")

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Pragmas applied to every new connection. Critical for the WAL + durability
# trade-off we accepted in plan.md §4.
_PRAGMAS = [
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
]


def _apply_pragmas_sync(conn: sqlite3.Connection) -> None:
    for pragma in _PRAGMAS:
        conn.execute(pragma)


async def _apply_pragmas(db: aiosqlite.Connection) -> None:
    for pragma in _PRAGMAS:
        await db.execute(pragma)


async def open_db() -> aiosqlite.Connection:
    """Open a connection with pragmas applied.

    The caller is responsible for closing it. For the long-running scheduler
    process we keep a single connection alive. For one-shot CLI commands
    (seed, check-once) we open + close per command.
    """
    db = await aiosqlite.connect(str(CONFIG.db_path))
    db.row_factory = aiosqlite.Row
    await _apply_pragmas(db)
    return db


async def run_migrations(db: aiosqlite.Connection) -> None:
    """Apply any migrations not yet recorded in schema_version.

    Migrations are .sql files in the migrations/ directory, named
    NNNN_description.sql. They are applied in lexical order.
    """
    # Bootstrap: ensure schema_version exists even before any migration runs.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    await db.commit()

    async with db.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version") as cur:
        row = await cur.fetchone()
        applied = int(row["v"])

    files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    for path in files:
        try:
            version = int(path.name.split("_", 1)[0])
        except ValueError:
            log.warning("Skipping migration file with non-numeric prefix: %s", path.name)
            continue
        if version <= applied:
            continue
        log.info("Applying migration %s", path.name)
        sql = path.read_text(encoding="utf-8")
        # aiosqlite doesn't expose executescript's multi-statement API reliably
        # across versions, so we execute the file as a single script.
        await db.executescript(sql)
        await db.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (version,)
        )
        await db.commit()
        log.info("Migration %s applied", path.name)


# ---------------------------------------------------------------------------
# Sync variant — used by tests and quick CLI helpers. Same pragmas.
# ---------------------------------------------------------------------------


def open_db_sync(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else CONFIG.db_path
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    _apply_pragmas_sync(conn)
    return conn


def run_migrations_sync(conn: sqlite3.Connection) -> None:
    """Sync version of run_migrations, used by tests."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()

    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version").fetchone()
    applied = int(row["v"])

    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        try:
            version = int(path.name.split("_", 1)[0])
        except ValueError:
            continue
        if version <= applied:
            continue
        sql = path.read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()


async def lifespan_helper() -> AsyncIterator[aiosqlite.Connection]:
    """Convenience context manager for CLI commands: open + migrate + close."""
    db = await open_db()
    try:
        await run_migrations(db)
        yield db
    finally:
        await db.close()


if __name__ == "__main__":
    # Allow `python -m engine.db` to apply migrations manually.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    async def _main() -> None:
        async with lifespan_helper() as _db:
            log.info("Migrations OK; database at %s", CONFIG.db_path)

    asyncio.run(_main())
