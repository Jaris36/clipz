"""Stage 1 to 3 orchestration: find new VODs, score them, store ranked moments."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from clipz.pipeline.config import PipelineSettings as Settings
from clipz.pipeline.rank import Moment, combine, pick_moments
from clipz.pipeline.signals.audio import audio_score
from clipz.pipeline.signals.chat_spikes import bucket_chat, chat_rate_score, emote_score
from clipz.pipeline.signals.viewer_clips import viewer_clip_score

log = logging.getLogger(__name__)


def analyze(duration_s: float, cfg: dict, chat_rows: list[tuple[float, str]] | None = None,
            clips: list[tuple[float, float, int]] | None = None,
            loudness: list[tuple[float, float]] | None = None) -> list[Moment]:
    """Pure scoring: any signal may be missing; its weight is then simply unused."""
    window = float(cfg.get("window_seconds", 10))
    weights = dict(cfg.get("weights") or {"chat": 1.0, "emotes": 0.6, "viewer_clips": 1.5, "audio": 0.4})
    signals: dict[str, list[float]] = {}
    chat = None
    if chat_rows:
        chat = bucket_chat(chat_rows, duration_s, window, float(cfg.get("chat_lag_seconds", 8)),
                           cfg.get("tokens") or {})
        signals["chat"] = chat_rate_score(chat)
        signals["emotes"] = emote_score(chat)
    n = chat.n if chat else max(1, int(duration_s // window) + 1)
    if clips:
        signals["viewer_clips"] = viewer_clip_score(clips, n, window)
    if loudness:
        signals["audio"] = audio_score(loudness, n, window)
    if not signals:
        return []
    total = combine(signals, weights)
    return pick_moments(total, signals, weights, window, duration_s, cfg, chat)


def save_moments(conn: sqlite3.Connection, vod_id: int, moments: list[Moment]) -> None:
    """Store ranked moments and one `candidate` clip per moment (rendered in phase 2)."""
    info = conn.execute(
        """SELECT s.channel, s.platform, s.niche FROM vods v JOIN streamers s ON s.id = v.streamer_id
           WHERE v.id = ?""", (vod_id,)).fetchone()
    conn.execute("""DELETE FROM clips WHERE state = 'candidate'
                    AND moment_id IN (SELECT id FROM moments WHERE vod_id = ?)""", (vod_id,))
    conn.execute("""DELETE FROM moments WHERE vod_id = ?
                    AND id NOT IN (SELECT moment_id FROM clips WHERE moment_id IS NOT NULL)""", (vod_id,))
    for m in moments:
        cur = conn.execute(
            """INSERT INTO moments (vod_id, start_s, end_s, peak_s, score, score_chat, score_emotes,
                                    score_clips, score_audio, moment_type, sample_chat)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (vod_id, m.start_s, m.end_s, m.peak_s, m.score, m.parts.get("chat", 0), m.parts.get("emotes", 0),
             m.parts.get("viewer_clips", 0), m.parts.get("audio", 0), m.moment_type,
             json.dumps(m.sample_chat, ensure_ascii=False)),
        )
        conn.execute(
            """INSERT INTO clips (id, streamer, state, length_seconds, moment_score, moment_id,
                                  platform, niche, start_s, end_s, updated_at)
               VALUES (?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (f"m{cur.lastrowid}", info["channel"], round(m.duration_s, 1), m.score, cur.lastrowid,
             info["platform"], info["niche"], m.start_s, m.end_s))
    conn.execute("UPDATE vods SET status = 'ranked', processed_at = datetime('now') WHERE id = ?", (vod_id,))
    conn.commit()


def upsert_vod(conn: sqlite3.Connection, streamer_id: int, platform_vod_id: str, title: str, url: str,
               started_at: str | None, duration_s: int) -> tuple[int, bool]:
    row = conn.execute("SELECT id, status FROM vods WHERE streamer_id = ? AND platform_vod_id = ?",
                       (streamer_id, platform_vod_id)).fetchone()
    if row:
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO vods (streamer_id, platform_vod_id, title, url, started_at, duration_s) VALUES (?, ?, ?, ?, ?, ?)",
        (streamer_id, platform_vod_id, title, url, started_at, duration_s))
    conn.commit()
    return cur.lastrowid, True


def _fail(conn, vod_id, err):
    log.exception("vod %s failed", vod_id)
    conn.execute("UPDATE vods SET status = 'failed', error = ? WHERE id = ?", (str(err)[:500], vod_id))
    conn.commit()


def process_twitch_source(conn: sqlite3.Connection, settings: Settings, source: sqlite3.Row,
                          max_vods: int = 2, with_audio: bool = False) -> int:
    from clipz.pipeline.fetch.chat import download_twitch_chat, load_twitch_chat
    from clipz.pipeline.sources.twitch import TwitchAPI

    api = TwitchAPI(settings.twitch_client_id, settings.twitch_client_secret)
    user_id = source["platform_user_id"] or api.user_id(source["channel"])
    conn.execute("UPDATE streamers SET platform_user_id = ? WHERE id = ?", (user_id, source["id"]))
    done = 0
    for vod in api.recent_vods(user_id, limit=max_vods):
        vod_id, new = upsert_vod(conn, source["id"], vod.id, vod.title, vod.url, vod.started_at, vod.duration_s)
        if not new:
            continue
        try:
            chat_path = settings.data_dir / "chat" / "twitch" / f"{vod.id}.json"
            download_twitch_chat(vod.id, chat_path, settings.twitch_downloader_cli)
            conn.execute("UPDATE vods SET chat_path = ?, status = 'signals' WHERE id = ?", (str(chat_path), vod_id))
            clips = api.clips_for_vod(user_id, vod)
            for c in clips:
                conn.execute(
                    """INSERT OR IGNORE INTO viewer_clips (vod_id, platform_clip_id, title, view_count,
                       vod_offset_s, duration_s, url) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (vod_id, c.id, c.title, c.view_count, c.vod_offset_s, c.duration_s, c.url))
            loud = None
            if with_audio:
                from clipz.pipeline.fetch.segments import download_audio
                from clipz.pipeline.signals.audio import momentary_loudness
                audio_path = settings.data_dir / "audio" / f"twitch-{vod.id}.m4a"
                download_audio(vod.url, audio_path)
                loud = momentary_loudness(audio_path)
                audio_path.unlink(missing_ok=True)
            moments = analyze(vod.duration_s, settings.scoring, load_twitch_chat(chat_path),
                              [(c.vod_offset_s, c.duration_s, c.view_count) for c in clips], loud)
            save_moments(conn, vod_id, moments)
            done += 1
        except Exception as e:  # keep going with the next VOD
            _fail(conn, vod_id, e)
    return done


def process_kick_source(conn: sqlite3.Connection, settings: Settings, source: sqlite3.Row,
                        max_vods: int = 2) -> int:
    from clipz.pipeline.sources.kick import KickAPI, _parse_ts, estimate_offsets
    from clipz.pipeline.sources.kick_chat_recorder import load_kick_chat

    api = KickAPI(settings.kick_proxy)
    clips_all = api.recent_clips(source["channel"], time_range="week")
    done = 0
    for vod in api.recent_vods(source["channel"], limit=max_vods):
        vod_id, new = upsert_vod(conn, source["id"], vod.id, vod.title, vod.url, vod.started_at, vod.duration_s)
        if not new:
            continue
        try:
            chat_rows = None
            chat_path = kick_chat_path(settings, source["channel"], vod.livestream_id)
            start = _parse_ts(vod.started_at)
            if chat_path.exists() and start:
                chat_rows = load_kick_chat(chat_path, start.timestamp())
                conn.execute("UPDATE vods SET chat_path = ? WHERE id = ?", (str(chat_path), vod_id))
            clips = estimate_offsets(list(clips_all), vod)
            for c in clips:
                conn.execute(
                    """INSERT OR IGNORE INTO viewer_clips (vod_id, platform_clip_id, title, view_count,
                       vod_offset_s, duration_s, url) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (vod_id, c.id, c.title, c.view_count, c.vod_offset_s, c.duration_s, c.url))
            moments = analyze(vod.duration_s, settings.scoring, chat_rows,
                              [(c.vod_offset_s, c.duration_s, c.view_count) for c in clips])
            save_moments(conn, vod_id, moments)
            done += 1
        except Exception as e:
            _fail(conn, vod_id, e)
    return done


def kick_chat_path(settings: Settings, slug: str, livestream_id) -> Path:
    return settings.data_dir / "chat" / "kick" / slug / f"{livestream_id}.jsonl"


def watch_once(conn: sqlite3.Connection, settings: Settings) -> dict:
    """One hourly pass over every active source. Returns {channel: vods processed}."""
    results = {}
    for src in conn.execute("SELECT * FROM streamers WHERE active = 1").fetchall():
        key = f"{src['platform']}:{src['channel']}"
        try:
            if src["platform"] == "twitch":
                results[key] = process_twitch_source(conn, settings, src)
            else:
                results[key] = process_kick_source(conn, settings, src)
        except Exception as e:
            log.exception("source %s failed", key)
            results[key] = f"error: {e}"
    return results


def moments_report(moments: list[Moment]) -> str:
    def ts(s: float) -> str:
        s = int(s)
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    lines = [f"{'#':>2}  {'start':>8}  {'end':>8}  {'score':>6}  {'type':<12}  signals  |  chat sample"]
    for i, m in enumerate(moments, 1):
        parts = " ".join(f"{k}={v:.1f}" for k, v in m.parts.items() if v)
        sample = " / ".join(m.sample_chat[:3])[:90]
        lines.append(f"{i:>2}  {ts(m.start_s):>8}  {ts(m.end_s):>8}  {m.score:>6.2f}  {m.moment_type:<12}  {parts}  |  {sample}")
    return "\n".join(lines)


def moments_json(moments: list[Moment]) -> str:
    return json.dumps([asdict(m) for m in moments], indent=2, ensure_ascii=False)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
