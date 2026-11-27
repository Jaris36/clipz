"""Record Kick chat live, because Kick offers no chat-replay download.

Kick's web chat runs on Pusher. We subscribe to the public chatroom channel
read-only (no login, no sending) and append each message to a JSONL file:
{"t": <unix seconds>, "user": "...", "text": "..."}.
Offsets into the VOD are computed later from the stream's start time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import websockets

log = logging.getLogger(__name__)

# Kick's public Pusher app key (visible in kick.com's web client). Override if it changes.
DEFAULT_PUSHER_KEY = "32cbd69e4b950bf97679"
PUSHER_URL = "wss://ws-us2.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0&flash=false"
CHAT_EVENT = "App\\Events\\ChatMessageEvent"


def pusher_url() -> str:
    return PUSHER_URL.format(key=os.environ.get("KICK_PUSHER_KEY", DEFAULT_PUSHER_KEY))


def parse_chat_event(raw: str) -> dict | None:
    """Turn one Pusher frame into a chat record, or None if it isn't a chat message."""
    try:
        frame = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if frame.get("event") != CHAT_EVENT:
        return None
    data = frame.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    return {
        "t": time.time(),
        "created_at": data.get("created_at"),
        "user": (data.get("sender") or {}).get("username", ""),
        "text": data.get("content", ""),
    }


async def record(chatroom_id: int, out_path: Path, stop: asyncio.Event | None = None,
                 max_seconds: float | None = None) -> int:
    """Append chat to out_path until stop is set or max_seconds pass. Returns message count."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stop = stop or asyncio.Event()
    deadline = time.time() + max_seconds if max_seconds else None
    count = 0
    backoff = 1
    while not stop.is_set() and (deadline is None or time.time() < deadline):
        try:
            async with websockets.connect(pusher_url(), open_timeout=15, ping_interval=None) as ws:
                await ws.send(json.dumps({
                    "event": "pusher:subscribe",
                    "data": {"auth": "", "channel": f"chatrooms.{chatroom_id}.v2"},
                }))
                backoff = 1
                with out_path.open("a", encoding="utf-8") as f:
                    while not stop.is_set() and (deadline is None or time.time() < deadline):
                        timeout = 30 if deadline is None else max(0.1, min(30, deadline - time.time()))
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            await ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
                            continue
                        if '"pusher:ping"' in raw:
                            await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                            continue
                        rec = parse_chat_event(raw)
                        if rec:
                            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                            f.flush()
                            count += 1
        except (OSError, websockets.WebSocketException) as e:
            log.warning("kick chat %s disconnected: %s; retrying in %ss", chatroom_id, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
    return count


def load_kick_chat(path: Path, stream_started_unix: float) -> list[tuple[float, str]]:
    """Read a recorded JSONL file as (offset_seconds, text) pairs relative to stream start."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        offset = rec["t"] - stream_started_unix
        if offset >= 0:
            rows.append((offset, rec.get("text", "")))
    return rows
