from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id      TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    duration      INTEGER,
    first_seen    TEXT DEFAULT (datetime('now')),
    last_seen     TEXT DEFAULT (datetime('now')),
    clean_title   TEXT,
    alt_titles    TEXT,              -- JSON list
    year_hint     INTEGER,
    colorized     INTEGER DEFAULT 0,
    claimed_res   INTEGER,
    is_extra      INTEGER DEFAULT 0,
    extra_reason  TEXT,
    -- pending | matched | review | unmatched | manual | skipped | extra
    match_status  TEXT DEFAULT 'pending',
    tmdb_id       INTEGER,
    score         REAL,
    candidates    TEXT,              -- JSON list of top candidates
    matched_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_videos_tmdb ON videos(tmdb_id);

CREATE TABLE IF NOT EXISTS overrides (
    video_id  TEXT PRIMARY KEY,
    tmdb_id   INTEGER,               -- NULL with skip=1 means "not a movie / ignore"
    skip      INTEGER DEFAULT 0,
    note      TEXT,
    set_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tmdb_movies (
    tmdb_id    INTEGER PRIMARY KEY,
    json       TEXT NOT NULL,
    fetched_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS probes (
    video_id     TEXT PRIMARY KEY,
    probed_at    TEXT DEFAULT (datetime('now')),
    unavailable  INTEGER DEFAULT 0,
    height       INTEGER,
    width        INTEGER,
    vcodec       TEXT,
    acodec       TEXT,
    fps          REAL,
    filesize     INTEGER,
    error        TEXT
);

-- One row per movie (per edition when colorized_policy = keep_both) that is on the NAS.
CREATE TABLE IF NOT EXISTS library (
    tmdb_id    INTEGER NOT NULL,
    edition    TEXT NOT NULL DEFAULT '',
    video_id   TEXT NOT NULL,
    path       TEXT NOT NULL,
    height     INTEGER,
    vcodec     TEXT,
    duration   INTEGER,
    colorized  INTEGER DEFAULT 0,
    added_at   TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (tmdb_id, edition)
);

CREATE TABLE IF NOT EXISTS failures (
    video_id   TEXT NOT NULL,
    failed_at  TEXT DEFAULT (datetime('now')),
    error      TEXT
);

CREATE TABLE IF NOT EXISTS tmdb_search_cache (
    query_key  TEXT PRIMARY KEY,
    json       TEXT NOT NULL,
    fetched_at TEXT DEFAULT (datetime('now'))
);
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
