"""Twitch Helix API: users, archived VODs and viewer-made clips.

Needs a free Twitch developer app (TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET).
Docs: https://dev.twitch.tv/docs/api/reference/#get-clips
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from clipz.pipeline.net import http_client

HELIX = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"


def parse_twitch_duration(text: str) -> int:
    """'3h2m10s' -> 10930 seconds."""
    total = 0
    for value, unit in re.findall(r"(\d+)([hms])", text or ""):
        total += int(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


@dataclass
class TwitchVod:
    id: str
    title: str
    url: str
    started_at: str
    duration_s: int


@dataclass
class TwitchClip:
    id: str
    title: str
    view_count: int
    vod_offset_s: float | None
    duration_s: float
    url: str
    video_id: str


class TwitchAPI:
    def __init__(self, client_id: str, client_secret: str, client: httpx.Client | None = None):
        if not client_id or not client_secret:
            raise ValueError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are not set")
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = client or http_client()
        self._token: str | None = None
        self._token_expires = 0.0

    def _auth(self) -> dict:
        if not self._token or time.time() > self._token_expires - 60:
            r = self.http.post(TOKEN_URL, params={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            })
            r.raise_for_status()
            body = r.json()
            self._token = body["access_token"]
            self._token_expires = time.time() + body.get("expires_in", 3600)
        return {"Client-Id": self.client_id, "Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params: dict) -> dict:
        r = self.http.get(f"{HELIX}{path}", params=params, headers=self._auth())
        r.raise_for_status()
        return r.json()

    def user_id(self, login: str) -> str:
        data = self._get("/users", {"login": login})["data"]
        if not data:
            raise LookupError(f"Twitch user {login!r} not found")
        return data[0]["id"]

    def recent_vods(self, user_id: str, limit: int = 5) -> list[TwitchVod]:
        data = self._get("/videos", {"user_id": user_id, "type": "archive", "first": limit})["data"]
        return [
            TwitchVod(v["id"], v["title"], v["url"], v["created_at"], parse_twitch_duration(v["duration"]))
            for v in data
        ]

    def clips_for_vod(self, user_id: str, vod: TwitchVod, max_pages: int = 5) -> list[TwitchClip]:
        """Viewer clips made during this VOD, most viewed first."""
        start = datetime.fromisoformat(vod.started_at.replace("Z", "+00:00"))
        end = start + timedelta(seconds=vod.duration_s) + timedelta(hours=1)
        params = {
            "broadcaster_id": user_id,
            "started_at": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "ended_at": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "first": 100,
        }
        clips: list[TwitchClip] = []
        for _ in range(max_pages):
            body = self._get("/clips", params)
            for c in body["data"]:
                if c.get("video_id") != vod.id:
                    continue
                clips.append(TwitchClip(
                    id=c["id"], title=c.get("title", ""), view_count=c.get("view_count", 0),
                    vod_offset_s=c.get("vod_offset"), duration_s=c.get("duration", 30.0),
                    url=c.get("url", ""), video_id=c["video_id"],
                ))
            cursor = (body.get("pagination") or {}).get("cursor")
            if not cursor:
                break
            params["after"] = cursor
        clips.sort(key=lambda c: c.view_count, reverse=True)
        return clips
