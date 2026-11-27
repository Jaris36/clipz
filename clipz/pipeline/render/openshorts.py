"""Client for a self-hosted OpenShorts backend (https://github.com/mutonby/openshorts, MIT).

We send one pre-cut segment per moment and ask for exactly one clip back:
OpenShorts reframes it to 9:16 with face tracking, burns in captions and adds a
hook text overlay. Self-hosted OpenShorts reads GEMINI_API_KEY from its own
environment; we can also pass it per request with the X-Gemini-Key header.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import httpx


class OpenShortsError(RuntimeError):
    pass


@dataclass
class RenderedClip:
    video_url: str
    hook: str
    title: str
    start: float | None
    end: float | None
    raw: dict


class OpenShortsClient:
    def __init__(self, base_url: str, gemini_key: str | None = None,
                 client: httpx.Client | None = None, poll_seconds: float = 5.0):
        self.base = base_url.rstrip("/")
        headers = {"X-Gemini-Key": gemini_key} if gemini_key else {}
        self.http = client or httpx.Client(timeout=120, headers=headers)
        if client is not None and gemini_key:
            self.http.headers.update(headers)
        self.poll_seconds = poll_seconds

    def healthy(self) -> bool:
        try:
            return self.http.get(f"{self.base}/health").status_code == 200
        except httpx.HTTPError:
            return False

    def submit(self, video: Path, clip_min: int = 15, clip_max: int = 60,
               captions: bool = True, auto_hook: bool = True) -> str:
        """Upload one segment and start a job that returns a single vertical clip."""
        with open(video, "rb") as f:
            r = self.http.post(
                f"{self.base}/api/process",
                files={"file": (video.name, f, "video/mp4")},
                data={
                    "acknowledged": "true",       # we only clip streamers who gave permission
                    "output_format": "vertical",
                    "target_clips": "1",
                    "clip_min_seconds": str(clip_min),
                    "clip_max_seconds": str(clip_max),
                    "captions": "true" if captions else "false",
                    "auto_hook": "true" if auto_hook else "false",
                },
                timeout=600,
            )
        if r.status_code >= 400:
            raise OpenShortsError(f"submit failed: HTTP {r.status_code} {r.text[:300]}")
        job_id = r.json().get("job_id")
        if not job_id:
            raise OpenShortsError(f"submit returned no job_id: {r.text[:300]}")
        return job_id

    def wait(self, job_id: str, timeout: float = 1800) -> list[RenderedClip]:
        deadline = time.time() + timeout
        while True:
            r = self.http.get(f"{self.base}/api/status/{job_id}")
            if r.status_code >= 400:
                raise OpenShortsError(f"status failed: HTTP {r.status_code} {r.text[:200]}")
            body = r.json()
            status = body.get("status")
            if status == "completed":
                return [_parse_clip(c) for c in ((body.get("result") or {}).get("clips") or [])]
            if status == "failed":
                logs = body.get("logs") or []
                raise OpenShortsError(f"job {job_id} failed: {logs[-1] if logs else 'no log'}")
            if time.time() > deadline:
                raise OpenShortsError(f"job {job_id} still {status} after {timeout:.0f}s")
            time.sleep(self.poll_seconds)

    def download(self, clip: RenderedClip, dest: Path) -> Path:
        url = clip.raw.get("download_url") or clip.video_url
        if url.startswith("/"):
            url = self.base + url
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.http.stream("GET", url, timeout=600) as r:
            if r.status_code >= 400:
                raise OpenShortsError(f"download failed: HTTP {r.status_code}")
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return dest

    def transcript_words(self, job_id: str, clip_index: int = 0) -> list[dict]:
        """Word list for a rendered clip, used by the music flag. Empty if unavailable."""
        try:
            r = self.http.get(f"{self.base}/api/clip/{job_id}/{clip_index}/transcript")
            if r.status_code >= 400:
                return []
            body = r.json()
        except (httpx.HTTPError, ValueError):
            return []
        words = body.get("words")
        if isinstance(words, list):
            return words
        out = []
        for seg in body.get("segments") or []:
            out.extend(seg.get("words") or [])
        return out


def _parse_clip(c: dict) -> RenderedClip:
    return RenderedClip(
        video_url=c.get("video_url") or "",
        hook=(c.get("viral_hook_text") or c.get("hook_text") or "").strip(),
        title=(c.get("video_title_for_youtube_short") or c.get("title") or "").strip(),
        start=c.get("start"),
        end=c.get("end"),
        raw=c,
    )
