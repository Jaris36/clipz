"""The scout loop: fetch -> store -> read rules -> filter and score -> queue alerts.

Run once:        python -m clipz.scout.runner --once
Run forever:     python -m clipz.scout.runner          (every 10 minutes)

Never joins, posts or submits. Alerts are queued in the alerts table; the Telegram
notifier (Phase 2) sends them. Until then they print to the console.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from clipz import db
from clipz.settings import ROOT, Settings
from clipz.scout.matcher import pick_clips
from clipz.scout.models import Campaign, CampaignStatus
from clipz.scout.rules_reader import RulesReader, description_hash
from clipz.scout.scorer import Scorer, match_streamer
from clipz.scout.sources.base import CampaignSource
from clipz.scout.sources.manual import ManualSource

POLL_SECONDS = 600
BUDGET_WARNING_PCT = 15


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def upsert_campaign(conn: sqlite3.Connection, c: Campaign) -> None:
    conn.execute(
        """INSERT INTO campaigns (id, source, source_id, whop_bounty_id, title, description, description_hash,
               link, status_open, pay_per_1k_views, pay_per_submission, currency, budget_total, budget_paid_out,
               spots_remaining, unresolved_submissions, allowed_countries, created_at, deadline)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               title=excluded.title, description=excluded.description,
               description_hash=excluded.description_hash, link=excluded.link,
               status_open=excluded.status_open, pay_per_1k_views=excluded.pay_per_1k_views,
               pay_per_submission=excluded.pay_per_submission, budget_total=excluded.budget_total,
               budget_paid_out=excluded.budget_paid_out, spots_remaining=excluded.spots_remaining,
               unresolved_submissions=excluded.unresolved_submissions,
               allowed_countries=excluded.allowed_countries, deadline=excluded.deadline,
               last_seen_at=datetime('now')""",
        (c.id, c.source, c.source_id, c.source_id if c.source == "whop_bounty" else None, c.title,
         c.description, description_hash(c.title + "\n" + c.description), c.link, int(c.status_open),
         c.pay_per_1k_views, c.pay_per_submission, c.currency, c.budget_total, c.budget_paid_out,
         c.spots_remaining, c.unresolved_submissions, json.dumps(c.allowed_countries),
         _iso(c.created_at), _iso(c.deadline)),
    )


def recent_history(conn: sqlite3.Connection) -> list[Campaign]:
    rows = conn.execute(
        "SELECT id, source, source_id, title, pay_per_1k_views, pay_per_submission FROM campaigns "
        "WHERE first_seen_at >= datetime('now', '-30 days')"
    ).fetchall()
    return [Campaign(**dict(r)) for r in rows]


def run_once(conn: sqlite3.Connection, sources: list[CampaignSource], reader: RulesReader, scorer: Scorer,
             tz: str = "America/New_York", now: datetime | None = None) -> list[dict]:
    """One poll. Returns the alerts queued this run."""
    now = now or datetime.now(timezone.utc)
    seen: list[Campaign] = []
    for src in sources:
        try:
            seen.extend(src.fetch())
        except Exception as exc:   # one broken source must not stop the others
            print(f"[scout] source {src.name} failed: {type(exc).__name__}: {exc}")
    for c in seen:
        upsert_campaign(conn, c)
    conn.commit()

    history = recent_history(conn)
    local_hour = now.astimezone(ZoneInfo(tz)).hour
    queued: list[dict] = []
    for c in seen:
        row = conn.execute("SELECT status FROM campaigns WHERE id = ?", (c.id,)).fetchone()
        status = row["status"]

        if status == CampaignStatus.JOINED.value:
            pct = c.budget_left_pct
            if not c.status_open or (pct is not None and pct < BUDGET_WARNING_PCT):
                conn.execute("UPDATE campaigns SET status = ? WHERE id = ?", (CampaignStatus.DRAINED.value, c.id))
                conn.execute("INSERT INTO alerts (campaign_id, kind) VALUES (?, 'budget_warning')", (c.id,))
                queued.append({"campaign_id": c.id, "kind": "budget_warning", "title": c.title})
            continue
        if status != CampaignStatus.NEW.value:
            continue   # already alerted, skipped or drained

        rules = reader.read(c)
        streamer_guess, _ = match_streamer(c, rules, scorer.watchlist)
        clips = pick_clips(conn, streamer_guess, rules, now=now)
        result = scorer.score(c, rules, ready_clips=len(clips), history=history, now=now)
        conn.execute(
            "INSERT INTO scores (campaign_id, passed, score, breakdown, reasons) VALUES (?,?,?,?,?)",
            (c.id, int(result.passed), result.score, json.dumps(result.breakdown), json.dumps(result.reasons)),
        )
        conn.execute("UPDATE campaigns SET score = ?, streamer = ? WHERE id = ?",
                     (result.score, result.streamer, c.id))
        if not result.passed:
            conn.execute("UPDATE campaigns SET status = ? WHERE id = ?", (CampaignStatus.SKIPPED.value, c.id))
            continue
        kind = scorer.alert_kind(result.score, local_hour)
        if kind is None:
            continue   # stays "new" and is rescored next poll (budget or clips may change)
        clip_ids = [cl.id for cl in clips]
        conn.execute("INSERT INTO alerts (campaign_id, kind, clip_ids) VALUES (?,?,?)",
                     (c.id, kind, json.dumps(clip_ids)))
        conn.execute("UPDATE campaigns SET status = ? WHERE id = ?", (CampaignStatus.ALERTED.value, c.id))
        queued.append({"campaign_id": c.id, "kind": kind, "title": c.title, "score": result.score,
                       "clips": clip_ids, "reasons": result.reasons, "link": c.link})
    conn.commit()
    return queued


def build(settings: Settings) -> tuple[sqlite3.Connection, list[CampaignSource], RulesReader, Scorer]:
    conn = db.connect(settings.db_path)
    sources: list[CampaignSource] = [ManualSource(ROOT / "config" / "manual_campaigns.yaml")]
    if settings.whop_token:
        from clipz.scout.sources.whop_bounties import WhopBountiesSource
        sources.insert(0, WhopBountiesSource(settings.whop_token, country=settings.country))
    reader = RulesReader(conn, api_key=settings.anthropic_api_key)
    scorer = Scorer(settings.scoring, settings.watchlist, settings.country)
    return conn, sources, reader, scorer


def main() -> None:
    parser = argparse.ArgumentParser(description="CLIPZ campaign scout")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    args = parser.parse_args()
    settings = Settings.load()
    conn, sources, reader, scorer = build(settings)
    tz = settings.scoring.get("alerts", {}).get("timezone", "America/New_York")
    while True:
        for alert in run_once(conn, sources, reader, scorer, tz=tz):
            print(f"[scout] ALERT {json.dumps(alert)}")
        if args.once:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
