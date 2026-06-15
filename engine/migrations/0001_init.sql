-- Tree Frog Streams — initial schema
-- See plan.md §3.3 and §4.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Logical channels. One row per *unique* channel after consolidation.
CREATE TABLE IF NOT EXISTS channels (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_name     TEXT NOT NULL,           -- "bbc news" — match key
    display_name        TEXT NOT NULL,           -- "BBC News" — what the user sees
    tvg_id              TEXT,                    -- authoritative cross-source id
    tvg_name            TEXT,                    -- original tvg-name attribute
    group_title         TEXT NOT NULL DEFAULT 'Other',
    logo_url            TEXT,
    bouquet             TEXT NOT NULL DEFAULT 'Auto',
    status              TEXT NOT NULL DEFAULT 'online'
                            CHECK (status IN ('online', 'offline', 'disabled')),
    availability_pct    REAL NOT NULL DEFAULT 100.0,
    last_checked_at     TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (normalized_name)
);

CREATE INDEX IF NOT EXISTS idx_channels_tvg_id       ON channels(tvg_id);
CREATE INDEX IF NOT EXISTS idx_channels_status       ON channels(status);
CREATE INDEX IF NOT EXISTS idx_channels_bouquet      ON channels(bouquet);
CREATE INDEX IF NOT EXISTS idx_channels_group        ON channels(group_title);

-- Physical stream sources backing a channel.
-- A channel can have many streams (failover candidates).
CREATE TABLE IF NOT EXISTS streams (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    source_url          TEXT NOT NULL,
    source_label        TEXT,                    -- e.g., "Provider A" — derived from import source
    priority            INTEGER NOT NULL DEFAULT 100,  -- lower = preferred
    status              TEXT NOT NULL DEFAULT 'unknown'
                            CHECK (status IN ('online', 'offline', 'disabled', 'unknown')),
    last_ok_at          TEXT,
    offline_since       TEXT,
    last_checked_at     TEXT,
    last_error          TEXT,
    last_latency_ms     INTEGER,
    UNIQUE (source_url)
);

CREATE INDEX IF NOT EXISTS idx_streams_channel       ON streams(channel_id);
CREATE INDEX IF NOT EXISTS idx_streams_status        ON streams(status);
CREATE INDEX IF NOT EXISTS idx_streams_priority      ON streams(channel_id, priority);

-- Redirect tokens (short_token → primary stream URL).
-- Populated only after a stream is selected as the active winner.
CREATE TABLE IF NOT EXISTS redirects (
    token       TEXT PRIMARY KEY,
    channel_id  INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    stream_id   INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_redirects_channel ON redirects(channel_id);

-- EPG program guide mappings (XMLTV tvg-id → channel).
-- Programs themselves live in a separate epg_programs table; not in v1.
CREATE TABLE IF NOT EXISTS epg_channels (
    tvg_id        TEXT PRIMARY KEY,
    display_name  TEXT,
    icon_url      TEXT
);

-- Rolling health-check log. Used for availability% calculation.
-- We keep ~30 days then aggregate and drop.
CREATE TABLE IF NOT EXISTS health_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id   INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    checked_at  TEXT NOT NULL DEFAULT (datetime('now')),
    ok          INTEGER NOT NULL CHECK (ok IN (0, 1)),
    latency_ms  INTEGER,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_health_logs_stream_time
    ON health_logs(stream_id, checked_at);

-- Import audit. One row per import run, with counts of new/duplicate/dead.
CREATE TABLE IF NOT EXISTS imports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url    TEXT,
    source_label  TEXT,
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    channels_new  INTEGER NOT NULL DEFAULT 0,
    streams_new   INTEGER NOT NULL DEFAULT 0,
    duplicates    INTEGER NOT NULL DEFAULT 0,
    notes         TEXT
);
