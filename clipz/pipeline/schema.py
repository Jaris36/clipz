"""Pipeline tables, added to the shared database in clipz/db.py.

The scout's schema (clipz.db.SCHEMA) owns campaigns, campaign_rules, scores,
alerts, post_requests and submissions, and creates the minimum `clips` table.
This module adds the pipeline's own tables and extends `clips` with extra
columns. It never redefines anything the scout created.
"""
from __future__ import annotations

import sqlite3

from clipz import db as shared_db

PIPELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS streamers (
  id               INTEGER PRIMARY KEY,
  platform         TEXT NOT NULL CHECK (platform IN ('twitch', 'kick')),
  channel          TEXT NOT NULL,
  platform_user_id TEXT,
  niche            TEXT NOT NULL,
  permission_type  TEXT NOT NULL CHECK (permission_type IN ('campaign', 'clipper_program', 'direct_deal')),
  permission_proof TEXT NOT NULL,
  active           INTEGER NOT NULL DEFAULT 1,
  created_at       TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (platform, channel)
);

CREATE TABLE IF NOT EXISTS vods (
  id               INTEGER PRIMARY KEY,
  streamer_id      INTEGER NOT NULL REFERENCES streamers(id),
  platform_vod_id  TEXT NOT NULL,
  title            TEXT,
  url              TEXT NOT NULL,
  started_at       TEXT,
  duration_s       INTEGER,
  chat_path        TEXT,
  status           TEXT NOT NULL DEFAULT 'new'
                   CHECK (status IN ('new', 'signals', 'ranked', 'done', 'failed')),
  error            TEXT,
  discovered_at    TEXT NOT NULL DEFAULT (datetime('now')),
  processed_at     TEXT,
  UNIQUE (streamer_id, platform_vod_id)
);

CREATE TABLE IF NOT EXISTS viewer_clips (
  id               INTEGER PRIMARY KEY,
  vod_id           INTEGER NOT NULL REFERENCES vods(id),
  platform_clip_id TEXT NOT NULL,
  title            TEXT,
  view_count       INTEGER NOT NULL DEFAULT 0,
  vod_offset_s     REAL,
  duration_s       REAL,
  url              TEXT,
  UNIQUE (vod_id, platform_clip_id)
);

CREATE TABLE IF NOT EXISTS moments (
  id               INTEGER PRIMARY KEY,
  vod_id           INTEGER NOT NULL REFERENCES vods(id),
  start_s          REAL NOT NULL,
  end_s            REAL NOT NULL,
  peak_s           REAL NOT NULL,
  score            REAL NOT NULL,
  score_chat       REAL NOT NULL DEFAULT 0,
  score_emotes     REAL NOT NULL DEFAULT 0,
  score_clips      REAL NOT NULL DEFAULT 0,
  score_audio      REAL NOT NULL DEFAULT 0,
  moment_type      TEXT,     -- funny | hype | shock | clip_request | viewer_clip | mixed
  sample_chat      TEXT,     -- JSON list of chat lines near the peak, shown in review
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS posts (
  id               INTEGER PRIMARY KEY,
  clip_id          TEXT NOT NULL REFERENCES clips(id),
  post_request_id  INTEGER REFERENCES post_requests(id),
  campaign_id      TEXT REFERENCES campaigns(id),
  platform         TEXT NOT NULL CHECK (platform IN ('tiktok', 'instagram', 'youtube')),
  account          TEXT NOT NULL,
  provider_job_id  TEXT,
  url              TEXT,
  scheduled_at     TEXT,
  posted_at        TEXT,
  status           TEXT NOT NULL DEFAULT 'scheduled'
                   CHECK (status IN ('scheduled', 'posted', 'failed', 'removed')),
  UNIQUE (clip_id, platform, account)
);

CREATE TABLE IF NOT EXISTS metrics (
  id               INTEGER PRIMARY KEY,
  post_id          INTEGER NOT NULL REFERENCES posts(id),
  measured_at      TEXT NOT NULL DEFAULT (datetime('now')),
  age_hours        INTEGER NOT NULL,
  views            INTEGER,
  likes            INTEGER,
  comments         INTEGER,
  shares           INTEGER
);

CREATE INDEX IF NOT EXISTS idx_moments_vod ON moments(vod_id);
"""

# Columns the pipeline adds to the scout's minimal clips table.
CLIP_COLUMNS = {
    "moment_id": "INTEGER REFERENCES moments(id)",
    "platform": "TEXT",
    "niche": "TEXT",
    "start_s": "REAL",
    "end_s": "REAL",
    "variant_of": "TEXT",
    "segment_path": "TEXT",
    "caption": "TEXT",
    "music_flag": "INTEGER NOT NULL DEFAULT 0",
    "gambling_flag": "INTEGER NOT NULL DEFAULT 0",
    "reject_reason": "TEXT",
    "approved_at": "TEXT",
    "updated_at": "TEXT",
    # Phase 2: rendering
    "render_attempts": "INTEGER NOT NULL DEFAULT 0",
    "render_error": "TEXT",
    "openshorts_job": "TEXT",
    "rendered_at": "TEXT",
}

# Columns added to streamers after Phase 1.
STREAMER_COLUMNS = {
    "facecam": "TEXT",   # JSON {x, y, w, h} as fractions of the frame, optional
}


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(PIPELINE_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(clips)")}
    for name, ddl in CLIP_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE clips ADD COLUMN {name} {ddl}")
    have = {r[1] for r in conn.execute("PRAGMA table_info(streamers)")}
    for name, ddl in STREAMER_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE streamers ADD COLUMN {name} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_moment ON clips(moment_id)")
    conn.commit()


def connect(db_path) -> sqlite3.Connection:
    """Open the shared database (scout schema first), then add the pipeline's tables."""
    conn = shared_db.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout = 30000")
    ensure(conn)
    return conn
