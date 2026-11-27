"""Kick channel, VOD and clip lookups via kick.com's site API.

Kick has no official VOD or clip-search API yet, and kick.com sits behind
Cloudflare, so every call goes through clipz.net.kick_client (proxy-ready via
KICK_PROXY). Field names are read defensively because the site API changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from clipz.pipeline.net import kick_client

BASE = "https://kick.com/api/v2"


class KickBlocked(RuntimeError):
    """Kick answered 403/429 or a Cloudflare challenge instead of JSON."""


@dataclass
class KickChannel:
    slug: str
    channel_id: int
    user_id: int | None
    chatroom_id: int | None
    is_live: bool
    livestream_started_at: str | None
    livestream_id: int | None


@dataclass
class KickVod:
    id: str           # video uuid, used in https://kick.com/<slug>/videos/<uuid>
    livestream_id: int | None
    title: str
    url: str
    started_at: str | None
    duration_s: int
    source_m3u8: str | None


@dataclass
class KickClip:
    id: str
    title: str
    view_count: int
    duration_s: float
    created_at: str | None
    livestream_id: int | None
    url: str
    vod_offset_s: float | None = None  # filled by estimate_offsets()


def _parse_ts(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class KickAPI:
    def __init__(self, proxy: str | None = None, client: httpx.Client | None = None):
        self.http = client or kick_client(proxy)

    def _get(self, path: str, params: dict | None = None):
        r = self.http.get(f"{BASE}{path}", params=params)
        ctype = r.headers.get("content-type", "")
        if r.status_code in (403, 429, 503) or "text/html" in ctype:
            raise KickBlocked(f"GET {path}: HTTP {r.status_code} ({ctype or 'no content-type'})")
        r.raise_for_status()
        return r.json()

    def channel(self, slug: str) -> KickChannel:
        d = self._get(f"/channels/{slug}")
        live = d.get("livestream") or None
        return KickChannel(
            slug=d.get("slug", slug),
            channel_id=d["id"],
            user_id=d.get("user_id"),
            chatroom_id=(d.get("chatroom") or {}).get("id"),
            is_live=bool(live and live.get("is_live", True)),
            livestream_started_at=(live or {}).get("start_time") or (live or {}).get("created_at"),
            livestream_id=(live or {}).get("id"),
        )

    def recent_vods(self, slug: str, limit: int = 5) -> list[KickVod]:
        data = self._get(f"/channels/{slug}/videos")
        items = data if isinstance(data, list) else data.get("data", [])
        vods = []
        for v in items[:limit]:
            video = v.get("video") or {}
            uuid = video.get("uuid") or v.get("uuid") or str(v.get("id"))
            duration = v.get("duration") or 0
            vods.append(KickVod(
                id=uuid,
                livestream_id=v.get("id"),
                title=v.get("session_title") or v.get("title") or "",
                url=f"https://kick.com/{slug}/videos/{uuid}",
                started_at=v.get("start_time") or v.get("created_at"),
                duration_s=int(duration / 1000) if duration > 100000 else int(duration),
                source_m3u8=v.get("source"),
            ))
        return vods

    def recent_clips(self, slug: str, time_range: str = "day", sort: str = "view") -> list[KickClip]:
        data = self._get(f"/channels/{slug}/clips", {"sort": sort, "time": time_range})
        items = data.get("clips", data.get("data", [])) if isinstance(data, dict) else data
        clips = []
        for c in items:
            clips.append(KickClip(
                id=str(c.get("id")),
                title=c.get("title") or "",
                view_count=int(c.get("view_count") or c.get("views") or 0),
                duration_s=float(c.get("duration") or 30),
                created_at=c.get("created_at"),
                livestream_id=c.get("livestream_id"),
                url=c.get("clip_url") or c.get("video_url") or "",
            ))
        return clips


def estimate_offsets(clips: list[KickClip], vod: KickVod) -> list[KickClip]:
    """Kick clips carry no VOD offset. Estimate it as clip creation time minus
    stream start minus clip length (clips are cut from the moments just before
    the button press). Only clips from this VOD's livestream are kept."""
    start = _parse_ts(vod.started_at)
    kept = []
    for c in clips:
        if vod.livestream_id and c.livestream_id and c.livestream_id != vod.livestream_id:
            continue
        created = _parse_ts(c.created_at)
        if start and created:
            offset = (created - start).total_seconds() - c.duration_s
            if 0 <= offset <= vod.duration_s:
                c.vod_offset_s = offset
                kept.append(c)
    return kept
