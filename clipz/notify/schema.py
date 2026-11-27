"""Notifier tables, added to the shared database.

The scout's `alerts` table already holds campaign alerts (with sent_at). These tables add:
  clip_alerts    strong pipeline moments worth a look, one row per moment
  alert_choices  every button tap Preston makes (a record only; nothing acts on it automatically)
  notify_state   small key/value store (Telegram update offset, last digest time)
"""
from __future__ import annotations

import sqlite3

NOTIFY_SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    vod_id       INTEGER NOT NULL,
    peak_s       REAL NOT NULL,          -- moments get re-ranked, so (vod, peak) is the stable key
    moment_id    INTEGER,
    clip_id      TEXT,
    streamer     TEXT NOT NULL,
    platform     TEXT NOT NULL,
    score        REAL NOT NULL,
    moment_type  TEXT,
    start_s      REAL NOT NULL,
    end_s        REAL NOT NULL,
    vod_url      TEXT,
    vod_title    TEXT,
    sample_chat  TEXT NOT NULL DEFAULT '[]',
    sent_at      TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (vod_id, peak_s)
);

CREATE TABLE IF NOT EXISTS alert_choices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind  TEXT NOT NULL CHECK (target_kind IN ('campaign', 'clip')),
    target_id    TEXT NOT NULL,          -- campaigns.id or clip_alerts.id
    choice       TEXT NOT NULL CHECK (choice IN ('joined', 'interested', 'skip')),
    telegram_user_id TEXT,
    chosen_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notify_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(NOTIFY_SCHEMA)
    conn.commit()


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM notify_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO notify_state (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
