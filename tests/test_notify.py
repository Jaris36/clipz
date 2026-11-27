"""Phase 2 notifier tests. A fake Telegram records calls; nothing touches the network."""
import json
from datetime import datetime, timezone

import pytest

from clipz.notify import messages
from clipz.notify.notifier import Notifier
from clipz.notify.telegram import DeliveryUnknown, TelegramClient, TelegramError
from clipz.pipeline.schema import ensure as ensure_pipeline
from tests.conftest import add_clip

CHAT = "12345"
AFTERNOON = datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)   # 3 PM New York
NIGHT = datetime(2026, 11, 19, 5, 0, tzinfo=timezone.utc)        # midnight New York
EVENING = datetime(2026, 11, 18, 23, 30, tzinfo=timezone.utc)    # 6:30 PM New York
ALERTS = {"timezone": "America/New_York", "quiet_hours": [23, 8], "digest_hours": [9, 18]}


class FakeTelegram:
    def __init__(self, fail=None):
        self.sent, self.answers, self.edits, self.updates = [], [], [], []
        self.fail = fail

    def send_message(self, chat_id, text, buttons=None):
        if self.fail:
            raise self.fail
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons})
        return len(self.sent)

    def get_updates(self, offset, timeout=0):
        self.last_offset = offset
        return [u for u in self.updates if offset is None or u["update_id"] >= offset]

    def answer_callback(self, callback_id, text=""):
        self.answers.append(text)

    def edit_buttons(self, chat_id, message_id, buttons):
        self.edits.append(buttons)


def add_campaign(conn, cid, title="GTA RP clips", status="alerted", score=82.0, link="https://whop.com/x"):
    conn.execute(
        """INSERT INTO campaigns (id, source, source_id, title, link, status, score, pay_per_1k_views,
               budget_total, budget_paid_out, streamer) VALUES (?, 'manual', ?, ?, ?, ?, ?, 1.5, 10000, 2000, 'streamer_gta_one')""",
        (cid, cid, title, link, status, score))
    conn.execute("INSERT INTO scores (campaign_id, passed, score, breakdown, reasons) VALUES (?, 1, ?, '{}', ?)",
                 (cid, score, json.dumps(["tier 1 niche", "2 clips ready"])))


def add_alert(conn, cid, kind, clip_ids=()):
    conn.execute("INSERT INTO alerts (campaign_id, kind, clip_ids) VALUES (?,?,?)", (cid, kind, json.dumps(list(clip_ids))))
    conn.commit()


def make(conn, tg=None, clip_cfg=None):
    tg = tg or FakeTelegram()
    return Notifier(conn, tg, CHAT, ALERTS, clip_cfg or {"min_score": 4.0, "max_per_vod": 2, "max_per_day": 3}), tg


def tap(tg, update_id, data, chat=CHAT, message_id=1, buttons=None):
    kb = [[{"text": t, "callback_data": d} for t, d in row] for row in (buttons or [])]
    tg.updates.append({"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "data": data, "from": {"id": 999},
        "message": {"message_id": message_id, "chat": {"id": int(chat)}, "reply_markup": {"inline_keyboard": kb}}}})


# ---- campaign alerts ---------------------------------------------------

def test_instant_alert_sent_once_with_buttons_and_clips(conn):
    add_clip(conn, "c1", "streamer_gta_one")
    add_campaign(conn, "manual:a")
    add_alert(conn, "manual:a", "instant", ["c1"])
    n, tg = make(conn)
    assert n.run_once(AFTERNOON)["instant"] == 1
    msg = tg.sent[0]
    assert msg["chat_id"] == CHAT
    assert "GTA RP clips" in msg["text"] and "score 82/100" in msg["text"]
    assert "$1.50 per 1K views" in msg["text"] and "$8,000 of $10,000 left (80%)" in msg["text"]
    assert "hook c1" in msg["text"] and "join it yourself" in msg["text"]
    assert [d for _, d in msg["buttons"][0]] == ["c:joined:1", "c:interested:1", "c:skip:1"]
    n.run_once(AFTERNOON)
    assert len(tg.sent) == 1   # never twice


def test_html_is_escaped(conn):
    add_campaign(conn, "manual:a", title="<b>Win</b> & more")
    add_alert(conn, "manual:a", "instant")
    n, tg = make(conn)
    n.run_once(AFTERNOON)
    assert "&lt;b&gt;Win&lt;/b&gt; &amp; more" in tg.sent[0]["text"]


def test_refused_send_is_retried_next_pass(conn):
    add_campaign(conn, "manual:a")
    add_alert(conn, "manual:a", "instant")
    n, tg = make(conn, FakeTelegram(fail=TelegramError("chat not found")))
    assert n.run_once(AFTERNOON)["instant"] == 0
    assert conn.execute("SELECT sent_at FROM alerts").fetchone()[0] is None
    tg.fail = None
    assert n.run_once(AFTERNOON)["instant"] == 1


def test_unknown_delivery_is_not_resent(conn):
    add_campaign(conn, "manual:a")
    add_alert(conn, "manual:a", "instant")
    n, tg = make(conn, FakeTelegram(fail=DeliveryUnknown("timeout")))
    n.run_once(AFTERNOON)
    tg.fail = None
    n.run_once(AFTERNOON)
    assert tg.sent == []
    assert conn.execute("SELECT sent_at FROM alerts").fetchone()[0] is not None


def test_alert_dropped_if_already_skipped(conn):
    add_campaign(conn, "manual:a", status="skipped")
    add_alert(conn, "manual:a", "instant")
    n, tg = make(conn)
    n.run_once(AFTERNOON)
    assert tg.sent == []


def test_digest_batches_and_sends_once_per_slot(conn):
    for i in range(3):
        add_campaign(conn, f"manual:{i}", title=f"Camp {i}", score=60)
        add_alert(conn, f"manual:{i}", "digest")
    n, tg = make(conn)
    assert n.run_once(AFTERNOON)["digest"] == 3   # 9 AM slot already passed, nothing sent yet
    assert len(tg.sent) == 1 and "Campaign digest</b> (3)" in tg.sent[0]["text"]
    assert len(tg.sent[0]["buttons"]) == 3 and tg.sent[0]["buttons"][1][0][0] == "2. I joined"
    add_campaign(conn, "manual:9", title="Late", score=55)
    add_alert(conn, "manual:9", "digest")
    assert n.run_once(AFTERNOON)["digest"] == 0   # same slot: waits for 6 PM
    assert n.run_once(EVENING)["digest"] == 1


def test_digest_and_budget_warning_wait_out_quiet_hours(conn):
    add_campaign(conn, "manual:a", score=60)
    add_alert(conn, "manual:a", "digest")
    add_campaign(conn, "manual:b", status="drained")
    add_alert(conn, "manual:b", "budget_warning")
    n, tg = make(conn)
    assert n.run_once(NIGHT) == {"instant": 0, "digest": 0, "clips_queued": 0, "clips_sent": 0}
    out = n.run_once(AFTERNOON)
    assert out["instant"] == 1 and out["digest"] == 1
    assert any("Budget" in m["text"] for m in tg.sent)


def test_instant_still_sent_at_night(conn):
    # The scout already downgrades instant alerts at night unless score >= quiet_override.
    add_campaign(conn, "manual:a", score=95)
    add_alert(conn, "manual:a", "instant")
    n, tg = make(conn)
    assert n.run_once(NIGHT)["instant"] == 1


# ---- buttons -----------------------------------------------------------

def test_joined_button_marks_campaign_and_records_choice(conn):
    add_campaign(conn, "manual:a")
    n, tg = make(conn)
    tap(tg, 10, "c:joined:1", buttons=messages.campaign_alert(
        dict(conn.execute("SELECT rowid AS ref, * FROM campaigns").fetchone()), [], [])[1])
    assert n.handle_updates() == 1
    assert conn.execute("SELECT status FROM campaigns").fetchone()[0] == "joined"
    row = conn.execute("SELECT target_kind, target_id, choice, telegram_user_id FROM alert_choices").fetchone()
    assert tuple(row) == ("campaign", "manual:a", "joined", "999")
    assert tg.edits == [[[("✓ Joined", "done")]]]
    assert "Budget warnings are on" in tg.answers[0]
    n.handle_updates()
    assert tg.last_offset == 11   # the tap is not handled twice


def test_skip_and_interested(conn):
    add_campaign(conn, "manual:a")
    add_campaign(conn, "manual:b")
    n, tg = make(conn)
    tap(tg, 1, "c:skip:1")
    tap(tg, 2, "c:interested:2")
    assert n.handle_updates() == 2
    st = dict(conn.execute("SELECT id, status FROM campaigns").fetchall())
    assert st == {"manual:a": "skipped", "manual:b": "alerted"}


def test_taps_from_other_chats_are_ignored(conn):
    add_campaign(conn, "manual:a")
    n, tg = make(conn)
    tap(tg, 1, "c:joined:1", chat="666")
    assert n.handle_updates() == 0
    assert conn.execute("SELECT status FROM campaigns").fetchone()[0] == "alerted"
    assert conn.execute("SELECT COUNT(*) FROM alert_choices").fetchone()[0] == 0


def test_bad_callback_data_is_ignored(conn):
    n, tg = make(conn)
    tap(tg, 1, "c:post:1")
    tap(tg, 2, "done")
    tap(tg, 3, "c:joined:99")
    assert n.handle_updates() == 0


def test_digest_tap_only_changes_its_row(conn):
    add_campaign(conn, "manual:a")
    add_campaign(conn, "manual:b")
    n, tg = make(conn)
    rows = [messages.campaign_buttons(1, "1. "), messages.campaign_buttons(2, "2. ")]
    tap(tg, 1, "c:skip:2", buttons=rows)
    n.handle_updates()
    assert tg.edits[0][0] == rows[0]
    assert tg.edits[0][1] == [("✓ 2. Skipped", "done")]


# ---- clip alerts -------------------------------------------------------

def add_vod(conn, platform="twitch", channel="streamer_gta_one"):
    ensure_pipeline(conn)
    cur = conn.execute("INSERT INTO streamers (platform, channel, niche, permission_type, permission_proof) "
                       "VALUES (?, ?, 'gta', 'campaign', 'https://x')", (platform, channel))
    sid = cur.lastrowid
    cur = conn.execute("INSERT INTO vods (streamer_id, platform_vod_id, title, url, duration_s, status) "
                       "VALUES (?, 'v1', 'Heist night', 'https://www.twitch.tv/videos/1', 7200, 'ranked')", (sid,))
    return cur.lastrowid


def add_moment(conn, vod_id, peak, score, created="2026-11-18 19:00:00"):
    conn.execute("INSERT INTO moments (vod_id, start_s, end_s, peak_s, score, moment_type, sample_chat, created_at) "
                 "VALUES (?, ?, ?, ?, ?, 'funny', ?, ?)",
                 (vod_id, peak - 30, peak + 30, peak, score, json.dumps(["KEKW", "LMAO <3"]), created))
    conn.commit()


def test_strong_moments_alert_with_timestamp_link(conn):
    vod = add_vod(conn)
    add_moment(conn, vod, 3725, 6.5)
    add_moment(conn, vod, 5000, 5.0)
    add_moment(conn, vod, 6000, 4.5)     # third strong one: over max_per_vod
    add_moment(conn, vod, 100, 2.0)      # below min_score
    n, tg = make(conn)
    out = n.run_once(AFTERNOON)
    assert out["clips_queued"] == 2 and out["clips_sent"] == 2
    first = tg.sent[0]["text"]
    assert "streamer_gta_one" in first and "1:01:35 to 1:02:35" in first
    assert "videos/1?t=1h1m35s" in first and "LMAO &lt;3" in first and "Nothing is posted" in first
    assert tg.sent[0]["buttons"][0][0][1].startswith("m:interested:")
    n.run_once(AFTERNOON)
    assert len(tg.sent) == 2


def test_clip_alerts_respect_quiet_hours_age_and_daily_cap(conn):
    vod = add_vod(conn)
    add_moment(conn, vod, 900, 9.0, created="2026-11-10 00:00:00")   # too old
    n, tg = make(conn, clip_cfg={"min_score": 4.0, "max_per_vod": 10, "max_per_day": 3})
    for i in range(5):
        add_moment(conn, vod, 1000 + i * 100, 5.0 + i)
    assert n.run_once(NIGHT)["clips_sent"] == 0
    assert n.run_once(AFTERNOON)["clips_sent"] == 3
    assert n.run_once(AFTERNOON)["clips_sent"] == 0


def test_clip_tap_recorded(conn):
    vod = add_vod(conn)
    add_moment(conn, vod, 3725, 6.5)
    n, tg = make(conn)
    n.run_once(AFTERNOON)
    tap(tg, 1, "m:skip:1")
    tap(tg, 2, "m:joined:1")   # not a clip choice
    assert n.handle_updates() == 1
    assert tuple(conn.execute("SELECT target_kind, target_id, choice FROM alert_choices").fetchone()) == ("clip", "1", "skip")


def test_no_pipeline_tables_is_fine(conn):
    n, _ = make(conn)
    assert n.run_once(AFTERNOON)["clips_queued"] == 0


# ---- the real client, with a fake HTTP layer ----------------------------

class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeHTTP:
    def __init__(self, response=None, exc=None):
        self.calls, self.response, self.exc = [], response, exc

    def post(self, url, json=None):
        self.calls.append((url, json))
        if self.exc:
            raise self.exc
        return self.response


def test_client_builds_send_message_request():
    http = FakeHTTP(FakeResponse(200, {"ok": True, "result": {"message_id": 7}}))
    c = TelegramClient("TOKEN", http=http)
    assert c.send_message("1", "hi", [[("A", "c:joined:1")]]) == 7
    url, body = http.calls[0]
    assert url == "https://api.telegram.org/botTOKEN/sendMessage"
    assert body["parse_mode"] == "HTML" and body["reply_markup"] == {
        "inline_keyboard": [[{"text": "A", "callback_data": "c:joined:1"}]]}


def test_client_errors_hide_token_and_split_unknown_delivery():
    import httpx
    c = TelegramClient("SECRET", http=FakeHTTP(FakeResponse(400, {"ok": False, "description": "chat not found"})))
    with pytest.raises(TelegramError) as e:
        c.send_message("1", "hi")
    assert "SECRET" not in str(e.value) and "chat not found" in str(e.value)
    c = TelegramClient("SECRET", http=FakeHTTP(exc=httpx.ReadTimeout("slow")))
    with pytest.raises(DeliveryUnknown):
        c.send_message("1", "hi")
    with pytest.raises(TelegramError):
        c.get_updates(None)


def test_callback_data_fits_telegram_limit():
    for row in [messages.campaign_buttons(10**9, "10. "), messages.clip_buttons(10**9)]:
        assert all(len(d.encode()) <= 64 for _, d in row)
