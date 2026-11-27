"""Render stage: candidate clips -> rendered (ready for review) or rejected.

For each candidate, best moments first:
1. download just that moment's segment of the VOD,
2. gambling niche: crop to the face camera so no casino screen is shown,
3. send it to OpenShorts for the 9:16 reframe, captions and hook,
4. check the result: casino text (gambling) and the music flag,
5. save the file and hook, and mark the clip rendered, or rejected with a reason.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from clipz.pipeline.config import PipelineSettings
from clipz.pipeline.render.guards import casino_text_hits, integrated_loudness, is_music_heavy
from clipz.pipeline.render.openshorts import OpenShortsClient

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


@dataclass
class RenderOutcome:
    clip_id: str
    state: str          # rendered | rejected | candidate (will retry)
    detail: str


def pending(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT c.id AS clip_id, c.start_s, c.end_s, c.moment_score, c.render_attempts,
                  v.platform_vod_id, v.url AS vod_url, s.platform, s.channel, s.niche, s.facecam
           FROM clips c
           JOIN moments m ON m.id = c.moment_id
           JOIN vods v ON v.id = m.vod_id
           JOIN streamers s ON s.id = v.streamer_id
           WHERE c.state = 'candidate' AND c.render_attempts < ? AND s.active = 1
           ORDER BY v.started_at DESC, c.moment_score DESC
           LIMIT ?""", (MAX_ATTEMPTS, limit)).fetchall()


def default_downloader(settings: PipelineSettings):
    from clipz.pipeline.fetch.segments import download_segment, download_twitch_segment

    def fetch(row: sqlite3.Row, out: Path) -> Path:
        height = int(settings.render.get("segment_height", 1080))
        if row["platform"] == "twitch":
            return download_twitch_segment(row["platform_vod_id"], row["start_s"], row["end_s"], out,
                                           cli=settings.twitch_downloader_cli)
        return download_segment(row["vod_url"], row["start_s"], row["end_s"], out,
                                max_height=height, proxy=settings.kick_proxy or None)
    return fetch


def render_one(conn: sqlite3.Connection, settings: PipelineSettings, row: sqlite3.Row,
               client: OpenShortsClient, fetch, facecam_fn=None, ocr=None, loudness_fn=None) -> RenderOutcome:
    from clipz.pipeline.render import facecam as fc

    cfg = settings.render
    facecam_fn = facecam_fn or fc.facecam_only
    loudness_fn = loudness_fn or integrated_loudness
    clip_id = row["clip_id"]
    work = settings.data_dir / "segments"
    seg = work / f"{clip_id}.mp4"
    gambling = row["niche"] in set(cfg.get("gambling_niches") or [])
    conn.execute("UPDATE clips SET render_attempts = render_attempts + 1, updated_at = datetime('now') WHERE id = ?",
                 (clip_id,))
    conn.commit()
    try:
        fetch(row, seg)
        source = seg
        if gambling:
            fc_cfg = cfg.get("facecam") or {}
            try:
                source, box = facecam_fn(seg, work / f"{clip_id}-facecam.mp4",
                                         json.loads(row["facecam"]) if row["facecam"] else None,
                                         int(fc_cfg.get("sample_frames", 12)), float(fc_cfg.get("padding", 0.35)))
                log.info("clip %s: cropped to facecam %s", clip_id, box)
            except fc.FacecamNotFound as e:
                return _reject(conn, clip_id, f"no face camera found ({e}); set facecam in watchlist.yaml")

        job_id = client.submit(source, int(cfg.get("clip_min_seconds", 15)), int(cfg.get("clip_max_seconds", 60)),
                               bool(cfg.get("captions", True)), bool(cfg.get("auto_hook", True)))
        clips = client.wait(job_id, float(cfg.get("job_timeout_seconds", 1800)))
        if not clips:
            return _reject(conn, clip_id, "OpenShorts found no usable clip in this moment")
        best = clips[0]
        out = settings.data_dir / "clips" / f"{clip_id}.mp4"
        client.download(best, out)

        words = client.transcript_words(job_id, 0)
        caption = " ".join(str(w.get("word", w.get("text", ""))).strip() for w in words).strip()
        duration = (best.end - best.start) if best.start is not None and best.end is not None else 0.0
        music = is_music_heavy(words, duration, loudness_fn(out), cfg) if words else False

        if gambling:
            hits = casino_text_hits(out, best.hook, caption, cfg, **({"ocr": ocr} if ocr else {}))
            if hits:
                reason = "; ".join(f"{h.where}: {h.text!r}" for h in hits[:5])
                out.unlink(missing_ok=True)
                return _reject(conn, clip_id, f"casino text: {reason}", openshorts_job=job_id)

        conn.execute(
            """UPDATE clips SET state = 'rendered', file_path = ?, hook_title = ?, caption = ?,
                   music_flag = ?, gambling_flag = ?, openshorts_job = ?, render_error = NULL,
                   length_seconds = COALESCE(NULLIF(?, 0), length_seconds),
                   rendered_at = datetime('now'), updated_at = datetime('now')
               WHERE id = ?""",
            (str(out), best.hook or best.title, caption, int(music), int(gambling), job_id,
             round(duration, 1), clip_id))
        conn.commit()
        return RenderOutcome(clip_id, "rendered", f"{out.name}, hook {best.hook!r}" + (", music flag" if music else ""))
    except Exception as e:
        log.exception("clip %s render failed", clip_id)
        conn.execute("UPDATE clips SET render_error = ?, updated_at = datetime('now') WHERE id = ?",
                     (f"{type(e).__name__}: {e}"[:500], clip_id))
        conn.commit()
        return RenderOutcome(clip_id, "candidate", f"failed, will retry: {type(e).__name__}: {e}"[:300])
    finally:
        for p in (seg, work / f"{clip_id}-facecam.mp4"):
            p.unlink(missing_ok=True)


def _reject(conn, clip_id: str, reason: str, openshorts_job: str | None = None) -> RenderOutcome:
    conn.execute(
        """UPDATE clips SET state = 'rejected', reject_reason = ?, openshorts_job = COALESCE(?, openshorts_job),
               updated_at = datetime('now') WHERE id = ?""", (reason[:500], openshorts_job, clip_id))
    conn.commit()
    return RenderOutcome(clip_id, "rejected", reason)


def render_pending(conn: sqlite3.Connection, settings: PipelineSettings, limit: int | None = None,
                   client: OpenShortsClient | None = None, fetch=None) -> list[RenderOutcome]:
    limit = limit or int(settings.render.get("renders_per_run", 10))
    client = client or OpenShortsClient(settings.openshorts_url, settings.gemini_api_key or None)
    fetch = fetch or default_downloader(settings)
    return [render_one(conn, settings, row, client, fetch) for row in pending(conn, limit)]
