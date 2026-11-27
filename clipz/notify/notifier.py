"""Sends queued alerts to Telegram and records button taps.

What it sends
  instant         campaign alerts the scout queued (score >= instant_min_score)
  digest          50-74 scores, batched at the digest hours (default 9 AM and 6 PM local)
  budget_warning  a joined campaign is closing or under 15% budget (held during quiet hours)
  clip alerts     pipeline moments scoring >= clip_alerts.min_score (held during quiet hours)

What the buttons do: write a row to alert_choices, and for campaigns set campaigns.status
("I joined" -> joined, so the scout starts budget warnings; "Skip" -> skipped).
Nothing here joins a campaign, posts, or submits. Preston joins on Whop himself.

Delivery is at most once: an alert is marked sent before the request goes out. If Telegram
refuses it, the mark is undone and it is tried on the next pass. If the connection drops
mid-request we can't know whether it arrived, so it stays marked and is logged, never resent.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from clipz.notify import messages
from clipz.notify.schema import ensure, get_state, set_state
from clipz.notify.telegram import Client, DeliveryUnknown, TelegramError

log = logging.getLogger("clipz.notify")

CHOICES = {"joined", "interested", "skip"}
CHOICE_LABEL = {"joined": "Joined", "interested": "Interested", "skip": "Skipped"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Notifier:
    def __init__(self, conn: sqlite3.Connection, client: Client, chat_id: str,
                 alerts_cfg: dict | None = None, clip_cfg: dict | None = None):
        self.conn = conn
        self.client = client
        self.chat_id = str(chat_id or "")
        a = alerts_cfg or {}
        self.tz = ZoneInfo(a.get("timezone", "America/New_York"))
        self.quiet_hours = a.get("quiet_hours", [23, 8])
        self.digest_hours = sorted(a.get("digest_hours", [9, 18]))
        c = clip_cfg or {}
        self.clip_enabled = c.get("enabled", True)
        self.clip_min_score = float(c.get("min_score", 4.0))
        self.clip_max_per_vod = int(c.get("max_per_vod", 2))
        self.clip_max_per_day = int(c.get("max_per_day", 10))
        self.clip_max_age_h = float(c.get("max_age_hours", 48))
        ensure(conn)

    # ---- time helpers --------------------------------------------------
    def is_quiet(self, now: datetime) -> bool:
        start, end = self.quiet_hours
        h = now.astimezone(self.tz).hour
        return (h >= start or h < end) if start > end else (start <= h < end)

    def digest_slot(self, now: datetime) -> str | None:
        """The most recent digest time that has passed, e.g. '2026-11-27@18'."""
        local = now.astimezone(self.tz)
        passed = [h for h in self.digest_hours if h <= local.hour]
        if passed:
            return f"{local.date()}@{passed[-1]}"
        if not self.digest_hours:
            return None
        return f"{(local - timedelta(days=1)).date()}@{self.digest_hours[-1]}"

    # ---- delivery ------------------------------------------------------
    def _claim(self, table: str, ids: list[int], now: datetime) -> list[int]:
        got = []
        for i in ids:
            cur = self.conn.execute(f"UPDATE {table} SET sent_at = ? WHERE id = ? AND sent_at IS NULL",
                                    (_iso(now), i))
            if cur.rowcount == 1:
                got.append(i)
        self.conn.commit()
        return got

    def _release(self, table: str, ids: list[int]) -> None:
        self.conn.executemany(f"UPDATE {table} SET sent_at = NULL WHERE id = ?", [(i,) for i in ids])
        self.conn.commit()

    def _deliver(self, table: str, ids: list[int], text: str, buttons) -> bool:
        try:
            self.client.send_message(self.chat_id, text, buttons)
            return True
        except DeliveryUnknown as e:
            log.warning("%s %s may not have arrived (%s); not resending", table, ids, e)
            return True
        except TelegramError as e:
            log.error("Telegram refused %s %s: %s; will try next pass", table, ids, e)
            self._release(table, ids)
            return False

    # ---- campaign alerts -----------------------------------------------
    def _campaign_bits(self, alert: sqlite3.Row) -> tuple[dict, list[str], list[dict]]:
        c = dict(self.conn.execute("SELECT rowid AS ref, * FROM campaigns WHERE id = ?", (alert["campaign_id"],)).fetchone())
        s = self.conn.execute("SELECT reasons FROM scores WHERE campaign_id = ? ORDER BY id DESC LIMIT 1",
                              (c["id"],)).fetchone()
        reasons = json.loads(s["reasons"]) if s else []
        clip_ids = json.loads(alert["clip_ids"] or "[]")
        clips = []
        for cid in clip_ids:
            row = self.conn.execute("SELECT id, hook_title, length_seconds, moment_score FROM clips WHERE id = ?",
                                    (cid,)).fetchone()
            if row:
                clips.append(dict(row))
        return c, reasons, clips

    def _pending(self, kinds: tuple[str, ...]) -> list[sqlite3.Row]:
        q = ",".join("?" * len(kinds))
        return self.conn.execute(f"SELECT * FROM alerts WHERE sent_at IS NULL AND kind IN ({q}) ORDER BY id",
                                 kinds).fetchall()

    def send_instant(self, now: datetime) -> int:
        sent = 0
        kinds = ("instant",) if self.is_quiet(now) else ("instant", "budget_warning")
        for alert in self._pending(kinds):
            c, reasons, clips = self._campaign_bits(alert)
            if alert["kind"] == "instant" and c["status"] != "alerted":
                self._claim("alerts", [alert["id"]], now)   # Preston already acted on it; drop quietly
                continue
            text, buttons = (messages.campaign_alert(c, reasons, clips) if alert["kind"] == "instant"
                             else messages.budget_warning(c))
            if self._claim("alerts", [alert["id"]], now) and self._deliver("alerts", [alert["id"]], text, buttons):
                sent += 1
        return sent

    def send_digest(self, now: datetime) -> int:
        slot = self.digest_slot(now)
        if slot is None or self.is_quiet(now) or get_state(self.conn, "last_digest_slot") == slot:
            return 0
        pending = self._pending(("digest",))
        items, ids, stale = [], [], []
        for alert in pending:
            bits = self._campaign_bits(alert)
            if bits[0]["status"] != "alerted":
                stale.append(alert["id"])
                continue
            items.append(bits)
            ids.append(alert["id"])
        self._claim("alerts", stale, now)
        sent = 0
        ok = True
        size = messages.MAX_DIGEST_ITEMS
        for k in range(0, len(items), size):
            chunk_ids = self._claim("alerts", ids[k:k + size], now)
            if not chunk_ids:
                continue
            text, buttons = messages.digest(items[k:k + size])
            if self._deliver("alerts", chunk_ids, text, buttons):
                sent += len(chunk_ids)
            else:
                ok = False
        if ok:
            set_state(self.conn, "last_digest_slot", slot)
            self.conn.commit()
        return sent

    # ---- clip alerts ---------------------------------------------------
    def _has_pipeline_tables(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'moments'").fetchone() is not None

    def queue_clip_alerts(self, now: datetime) -> int:
        if not self.clip_enabled or not self._has_pipeline_tables():
            return 0
        since = _iso(now - timedelta(hours=self.clip_max_age_h))
        rows = self.conn.execute(
            """SELECT m.id AS moment_id, m.vod_id, m.peak_s, m.start_s, m.end_s, m.score, m.moment_type,
                      m.sample_chat, v.url AS vod_url, v.title AS vod_title, s.channel, s.platform,
                      (SELECT c.id FROM clips c WHERE c.moment_id = m.id LIMIT 1) AS clip_id
               FROM moments m JOIN vods v ON v.id = m.vod_id JOIN streamers s ON s.id = v.streamer_id
               WHERE m.score >= ? AND m.created_at >= ?
               ORDER BY m.vod_id, m.score DESC""", (self.clip_min_score, since)).fetchall()
        per_vod: dict[int, int] = {}
        added = 0
        for r in rows:
            already = self.conn.execute("SELECT COUNT(*) FROM clip_alerts WHERE vod_id = ?", (r["vod_id"],)).fetchone()[0]
            n = per_vod.setdefault(r["vod_id"], already)
            if n >= self.clip_max_per_vod:
                continue
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO clip_alerts (vod_id, peak_s, moment_id, clip_id, streamer, platform, score,
                       moment_type, start_s, end_s, vod_url, vod_title, sample_chat)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["vod_id"], r["peak_s"], r["moment_id"], r["clip_id"], r["channel"], r["platform"], r["score"],
                 r["moment_type"], r["start_s"], r["end_s"], r["vod_url"], r["vod_title"], r["sample_chat"] or "[]"))
            if cur.rowcount:
                per_vod[r["vod_id"]] += 1
                added += 1
        self.conn.commit()
        return added

    def send_clip_alerts(self, now: datetime) -> int:
        if self.is_quiet(now):
            return 0
        day_ago = _iso(now - timedelta(hours=24))
        used = self.conn.execute("SELECT COUNT(*) FROM clip_alerts WHERE sent_at >= ?", (day_ago,)).fetchone()[0]
        room = max(0, self.clip_max_per_day - used)
        rows = self.conn.execute("SELECT * FROM clip_alerts WHERE sent_at IS NULL ORDER BY score DESC LIMIT ?",
                                 (room,)).fetchall()
        sent = 0
        for r in rows:
            text, buttons = messages.clip_alert(dict(r))
            if self._claim("clip_alerts", [r["id"]], now) and self._deliver("clip_alerts", [r["id"]], text, buttons):
                sent += 1
        return sent

    # ---- one pass ------------------------------------------------------
    def run_once(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        return {
            "instant": self.send_instant(now),
            "digest": self.send_digest(now),
            "clips_queued": self.queue_clip_alerts(now),
            "clips_sent": self.send_clip_alerts(now),
        }

    # ---- button taps ---------------------------------------------------
    def handle_updates(self, timeout: int = 0) -> int:
        raw = get_state(self.conn, "telegram_offset")
        updates = self.client.get_updates(int(raw) if raw else None, timeout=timeout)
        handled = 0
        for u in updates:
            set_state(self.conn, "telegram_offset", str(u["update_id"] + 1))
            cq = u.get("callback_query")
            if cq and self._handle_callback(cq):
                handled += 1
            self.conn.commit()
        return handled

    def _handle_callback(self, cq: dict) -> bool:
        msg = cq.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        if not self.chat_id or chat != self.chat_id:
            log.warning("ignored a button tap from chat %s (not TELEGRAM_CHAT_ID)", chat)
            return False
        data = cq.get("data") or ""
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[0] not in ("c", "m") or parts[1] not in CHOICES:
            self._answer(cq["id"], "")
            return False
        kind, choice, target = parts
        if kind == "m" and choice == "joined":
            return False
        target_kind = "campaign" if kind == "c" else "clip"
        ref = target
        if target_kind == "campaign":
            row = self.conn.execute("SELECT id FROM campaigns WHERE rowid = ?", (target,)).fetchone()
            if not row:
                self._answer(cq["id"], "That campaign is no longer in the database.")
                return False
            target = row["id"]
            if choice == "joined":
                self.conn.execute("UPDATE campaigns SET status = 'joined' WHERE id = ? AND status IN ('new','alerted','skipped')",
                                  (target,))
            elif choice == "skip":
                self.conn.execute("UPDATE campaigns SET status = 'skipped' WHERE id = ? AND status IN ('new','alerted')",
                                  (target,))
        elif not self.conn.execute("SELECT 1 FROM clip_alerts WHERE id = ?", (target,)).fetchone():
            self._answer(cq["id"], "That clip alert is no longer in the database.")
            return False
        self.conn.execute("INSERT INTO alert_choices (target_kind, target_id, choice, telegram_user_id) VALUES (?,?,?,?)",
                          (target_kind, target, choice, str((cq.get("from") or {}).get("id", ""))))
        self.conn.commit()
        note = {"joined": "Saved: you joined. Budget warnings are on for this one.",
                "interested": "Saved as interested.", "skip": "Skipped."}[choice]
        self._answer(cq["id"], note)
        self._mark_buttons(msg, f"{kind}:", ref, choice)
        return True

    def _answer(self, callback_id: str, text: str) -> None:
        try:
            self.client.answer_callback(callback_id, text)
        except (TelegramError, DeliveryUnknown) as e:
            log.warning("answerCallbackQuery failed: %s", e)

    def _mark_buttons(self, msg: dict, prefix: str, target: str, choice: str) -> None:
        """Swap the tapped row for a single '✓ Joined' style button so the choice is visible."""
        rows = ((msg.get("reply_markup") or {}).get("inline_keyboard")) or []
        if not rows or "message_id" not in msg:
            return
        new = []
        for row in rows:
            datas = [b.get("callback_data", "") for b in row]
            if any(d.startswith(prefix) and d.endswith(":" + target) for d in datas):
                first = row[0].get("text", "")
                num = first.split(" ", 1)[0] + " " if first[:1].isdigit() else ""
                new.append([(f"✓ {num}{CHOICE_LABEL[choice]}", "done")])
            else:
                new.append([(b.get("text", ""), b.get("callback_data", "")) for b in row])
        try:
            self.client.edit_buttons(self.chat_id, msg["message_id"], new)
        except (TelegramError, DeliveryUnknown) as e:
            log.warning("could not update buttons: %s", e)
