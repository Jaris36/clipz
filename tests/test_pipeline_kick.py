"""Kick parsing: chat frames, clip offsets, blocked responses."""
import json

import httpx
import pytest

from clipz.pipeline.sources.kick import KickAPI, KickBlocked, KickClip, KickVod, estimate_offsets
from clipz.pipeline.sources.kick_chat_recorder import load_kick_chat, parse_chat_event
from clipz.pipeline.sources.twitch import parse_twitch_duration


def test_parse_chat_event():
    frame = json.dumps({"event": "App\\Events\\ChatMessageEvent", "channel": "chatrooms.1.v2",
                        "data": json.dumps({"content": "W BIG WIN", "created_at": "2026-11-18T20:00:00Z",
                                            "sender": {"username": "viewer1"}})})
    rec = parse_chat_event(frame)
    assert rec["text"] == "W BIG WIN" and rec["user"] == "viewer1"
    assert parse_chat_event(json.dumps({"event": "pusher:connection_established", "data": "{}"})) is None
    assert parse_chat_event("not json") is None


def test_load_kick_chat(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text("\n".join(json.dumps({"t": 1000 + i, "text": f"m{i}"}) for i in range(-2, 3)) + "\nbroken\n")
    assert load_kick_chat(p, 1000) == [(0, "m0"), (1, "m1"), (2, "m2")]


def test_estimate_offsets_keeps_only_this_stream():
    vod = KickVod("uuid", 55, "t", "u", "2026-11-18T18:00:00Z", 3 * 3600, None)
    clips = [
        KickClip("a", "", 100, 30, "2026-11-18T19:00:30Z", 55, ""),
        KickClip("b", "", 100, 30, "2026-11-18T19:00:30Z", 99, ""),     # other stream
        KickClip("c", "", 100, 30, "2026-11-19T02:00:00Z", 55, ""),     # after the VOD ends
    ]
    kept = estimate_offsets(clips, vod)
    assert [c.id for c in kept] == ["a"] and kept[0].vod_offset_s == 3600


def test_kick_blocked_is_reported():
    def handler(request):
        return httpx.Response(403, headers={"content-type": "text/html"}, text="<html>Just a moment...</html>")
    api = KickAPI(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(KickBlocked):
        api.channel("someone")


def test_kick_channel_and_vods_parse():
    def handler(request):
        if request.url.path.endswith("/videos"):
            return httpx.Response(200, json=[{"id": 55, "session_title": "Slots", "start_time": "2026-11-18 18:00:00",
                                              "duration": 7_200_000, "video": {"uuid": "vid-uuid"}, "source": "m.m3u8"}])
        return httpx.Response(200, json={"id": 1, "slug": "slotsguy", "user_id": 2, "chatroom": {"id": 3},
                                         "livestream": {"id": 55, "is_live": True, "start_time": "2026-11-18 18:00:00"}})
    api = KickAPI(client=httpx.Client(transport=httpx.MockTransport(handler)))
    ch = api.channel("slotsguy")
    assert ch.chatroom_id == 3 and ch.is_live and ch.livestream_id == 55
    vod = api.recent_vods("slotsguy")[0]
    assert vod.id == "vid-uuid" and vod.duration_s == 7200 and vod.url.endswith("/videos/vid-uuid")


def test_twitch_duration():
    assert parse_twitch_duration("3h2m10s") == 10930
    assert parse_twitch_duration("45s") == 45
