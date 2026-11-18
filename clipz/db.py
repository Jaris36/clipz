"""SQLite database shared by the scout and the clipping pipeline.

The scout owns: campaigns, campaign_rules, scores, alerts, post_requests, submissions.
The clipping pipeline owns: clips. The scout only reads banked clips and marks them used,
so the clips table below is the minimum both sides agreed on; the pipeline may add columns.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id                     TEXT PRIMARY KEY,      -- "<source>:<source_id>"
    source                 TEXT NOT NULL,         -- whop_bounty | manual | apify
    source_id              TEXT NOT NULL,
    whop_bounty_id         TEXT,                  -- set when source = whop_bounty
    title                  TEXT NOT NULL,
    description            TEXT NOT NULL DEFAULT '',
    description_hash       TEXT NOT NULL DEFAULT '',
    link                   TEXT,
    status_open            INTEGER NOT NULL DEFAULT 1,
    pay_per_1k_views       REAL,
    pay_per_submission     REAL,
    currency               TEXT NOT NULL DEFAULT 'usd',
    budget_total           REAL,
    budget_paid_out        REAL,
    spots_remaining        INTEGER,
    unresolved_submissions INTEGER,
    allowed_countries      TEXT NOT NULL DEFAULT '[]',   -- JSON list
    created_at             TEXT,
    deadline               TEXT,
    streamer               TEXT,                  -- matched watchlist streamer, if any
    score                  REAL,
    status                 TEXT NOT NULL DEFAULT 'new',  -- new | alerted | joined | skipped | drained
    first_seen_at          TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS campaign_rules (
    campaign_id      TEXT PRIMARY KEY REFERENCES campaigns(id),
    description_hash TEXT NOT NULL,     -- rules are re-read only when the brief changes
    rules_json       TEXT NOT NULL,
    read_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scores (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  TEXT NOT NULL REFERENCES campaigns(id),
    passed       INTEGER NOT NULL,
    score        REAL NOT NULL,
    breakdown    TEXT NOT NULL,          -- JSON
    reasons      TEXT NOT NULL,          -- JSON list
    scored_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  TEXT NOT NULL REFERENCES campaigns(id),
    kind         TEXT NOT NULL,          -- instant | digest | budget_warning
    clip_ids     TEXT NOT NULL DEFAULT '[]',
    sent_at      TEXT,                   -- null until the notifier sends it
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Written only after Preston taps Approve. The pipeline posts it via Upload-Post
-- and writes the live URLs back; the scout then submits them.
CREATE TABLE IF NOT EXISTS post_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id      TEXT NOT NULL,
    campaign_id  TEXT NOT NULL REFERENCES campaigns(id),
    platforms    TEXT NOT NULL,          -- JSON list
    caption_tags TEXT NOT NULL DEFAULT '[]',
    disclosure   TEXT,
    approved_at  TEXT NOT NULL DEFAULT (datetime('now')),
    live_urls    TEXT,                   -- JSON list, written by the pipeline
    posted_at    TEXT
);

CREATE TABLE IF NOT EXISTS submissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_request_id INTEGER NOT NULL REFERENCES post_requests(id),
    whop_submission_id TEXT,
    status          TEXT NOT NULL,       -- submitted | manual_needed | failed
    submitted_at    TEXT NOT NULL DEFAULT (datetime('now')),
    error           TEXT
);

-- Owned by the clipping pipeline; minimum agreed columns.
CREATE TABLE IF NOT EXISTS clips (
    id              TEXT PRIMARY KEY,
    streamer        TEXT NOT NULL,
    state           TEXT NOT NULL,       -- candidate | rendered | banked | scheduled | posted | submitted
    length_seconds  REAL NOT NULL,
    moment_score    REAL NOT NULL DEFAULT 0,
    hook_title      TEXT NOT NULL DEFAULT '',
    file_path       TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
CREATE INDEX IF NOT EXISTS idx_clips_bank ON clips(streamer, state);
"""


def connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn
