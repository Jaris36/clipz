"""Reads ready clips from the clip bank for a campaign."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from clipz.scout.models import Clip, Rules

BANK_MAX_AGE_DAYS = 14   # agreed with the pipeline design


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def banked_clips(conn: sqlite3.Connection, streamer: str, now: datetime | None = None) -> list[Clip]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=BANK_MAX_AGE_DAYS)
    rows = conn.execute(
        "SELECT * FROM clips WHERE state = 'banked' AND lower(streamer) = lower(?) ORDER BY moment_score DESC",
        (streamer,),
    ).fetchall()
    clips = []
    for r in rows:
        cut_at = _parse_ts(r["created_at"])
        if cut_at < cutoff:
            continue
        clips.append(Clip(id=r["id"], streamer=r["streamer"], length_seconds=r["length_seconds"],
                          moment_score=r["moment_score"], hook_title=r["hook_title"],
                          file_path=r["file_path"], cut_at=cut_at))
    return clips


def fits_rules(clip: Clip, rules: Rules) -> bool:
    if rules.min_length_seconds and clip.length_seconds < rules.min_length_seconds:
        return False
    if rules.max_length_seconds and clip.length_seconds > rules.max_length_seconds:
        return False
    return True


def pick_clips(conn: sqlite3.Connection, streamer: str | None, rules: Rules, n: int = 3,
               now: datetime | None = None) -> list[Clip]:
    if not streamer:
        return []
    return [c for c in banked_clips(conn, streamer, now) if fits_rules(c, rules)][:n]
