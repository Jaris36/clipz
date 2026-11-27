"""Day-one smoke test: can this server reach Kick and Twitch?

Run on the VPS before anything else:  python -m clipz.pipeline smoke --kick <slug> --twitch <login>
Each check prints PASS, FAIL or SKIP with the reason. Nothing is posted or sent.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from clipz.pipeline.config import PipelineSettings as Settings
from clipz.pipeline.fetch.segments import download_segment, ytdlp_cmd


class Result:
    def __init__(self, name: str, status: str, detail: str):
        self.name, self.status, self.detail = name, status, detail

    def __str__(self) -> str:
        return f"[{self.status:<4}] {self.name}: {self.detail}"


def _check(name: str, fn) -> Result:
    t0 = time.time()
    try:
        detail = fn()
        return Result(name, "PASS", f"{detail} ({time.time() - t0:.1f}s)")
    except SkipCheck as e:
        return Result(name, "SKIP", str(e))
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"")
        err = err.decode(errors="replace") if isinstance(err, bytes) else err
        last = [line for line in err.strip().splitlines() if line.strip()][-1:] or ["no error output"]
        return Result(name, "FAIL", f"{Path(e.cmd[0]).name} exited {e.returncode}: {last[0][:300]}")
    except Exception as e:  # report every failure, keep going
        return Result(name, "FAIL", f"{type(e).__name__}: {str(e)[:300]}")


class SkipCheck(Exception):
    pass


def _first_entry(url: str, proxy: str | None = None) -> dict:
    cmd = ytdlp_cmd() + ["--flat-playlist", "-I", "1", "-J", "--quiet"]
    if proxy:
        cmd += ["--proxy", proxy]
    out = subprocess.run(cmd + [url], check=True, capture_output=True, text=True, timeout=120)  # noqa: S603
    doc = json.loads(out.stdout)
    entries = doc.get("entries") or []
    if not entries:
        raise RuntimeError(f"no entries at {url}")
    return entries[0]


def _segment_ok(url: str, tmp: Path, name: str, start: float, end: float, proxy: str | None = None) -> str:
    out = tmp / f"{name}.mp4"
    download_segment(url, start, end, out, max_height=720, proxy=proxy, timeout=300)
    size = out.stat().st_size if out.exists() else 0
    if size < 10_000:
        raise RuntimeError(f"downloaded file is only {size} bytes")
    return f"{end - start:.0f}s segment, {size / 1e6:.1f} MB"


def run(settings: Settings, kick_slug: str, twitch_login: str) -> list[Result]:
    from clipz.pipeline.sources.kick import KickAPI
    from clipz.pipeline.sources.kick_chat_recorder import record

    results: list[Result] = []
    proxy = settings.kick_proxy or None
    kick = KickAPI(proxy)
    state: dict = {}

    def kick_channel():
        ch = kick.channel(kick_slug)
        state["kick_channel"] = ch
        return f"channel {ch.slug} id={ch.channel_id} chatroom={ch.chatroom_id} live={ch.is_live}"
    results.append(_check("Kick channel API", kick_channel))

    def kick_chat():
        ch = state.get("kick_channel")
        if not ch or not ch.chatroom_id:
            raise SkipCheck("no chatroom id (channel lookup failed)")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "chat.jsonl"
            n = asyncio.run(record(ch.chatroom_id, path, max_seconds=25))
        if ch.is_live and n == 0:
            raise RuntimeError("connected but received no messages in 25s while the channel is live")
        return f"{n} messages in 25s" + ("" if ch.is_live else " (channel offline, connection only)")
    results.append(_check("Kick chat connection", kick_chat))

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        def kick_vods():
            vods = kick.recent_vods(kick_slug, limit=1)
            if not vods:
                raise RuntimeError("channel has no VODs listed")
            state["kick_vod"] = vods[0]
            return f"latest VOD {vods[0].url} ({vods[0].duration_s // 60} min)"
        results.append(_check("Kick VOD list", kick_vods))

        def kick_vod_download():
            vod = state.get("kick_vod")
            if not vod:
                raise SkipCheck("no VOD found")
            return _segment_ok(vod.url, tmp, "kick", 600, 615, proxy)
        results.append(_check("Kick VOD segment download", kick_vod_download))

        def kick_clips():
            clips = kick.recent_clips(kick_slug, time_range="week")
            return f"{len(clips)} clips this week" + (f", top has {clips[0].view_count} views" if clips else "")
        results.append(_check("Kick clips API", kick_clips))

        def twitch_api():
            if not (settings.twitch_client_id and settings.twitch_client_secret):
                raise SkipCheck("TWITCH_CLIENT_ID/SECRET not set yet")
            from clipz.pipeline.sources.twitch import TwitchAPI
            api = TwitchAPI(settings.twitch_client_id, settings.twitch_client_secret)
            uid = api.user_id(twitch_login)
            vods = api.recent_vods(uid, limit=1)
            clips = api.clips_for_vod(uid, vods[0]) if vods else []
            with_offset = sum(1 for c in clips if c.vod_offset_s is not None)
            return f"user {uid}, {len(vods)} VOD, {len(clips)} clips ({with_offset} with VOD offset)"
        results.append(_check("Twitch Helix API (clips with VOD offsets)", twitch_api))

        def twitch_clip():
            e = _first_entry(f"https://www.twitch.tv/{twitch_login}/clips?filter=clips&range=7d")
            url = e.get("url") or e.get("webpage_url")
            out = tmp / "twitch-clip.mp4"
            subprocess.run(ytdlp_cmd() + ["--quiet", "-f", "best[height<=720]/best", "-o", str(out), url],
                           check=True, capture_output=True, timeout=180)
            return f"downloaded clip {url}, {out.stat().st_size / 1e6:.1f} MB"
        results.append(_check("Twitch clip download", twitch_clip))

        def twitch_segment():
            e = _first_entry(f"https://www.twitch.tv/{twitch_login}/videos?filter=archives&sort=time")
            url = e.get("url") or e.get("webpage_url")
            state["twitch_vod_url"] = url
            return f"{url}: " + _segment_ok(url, tmp, "twitch", 600, 615)
        results.append(_check("Twitch VOD segment download", twitch_segment))

        def twitch_chat():
            cli = settings.twitch_downloader_cli
            if not shutil.which(cli):
                raise SkipCheck(f"{cli} not installed here (it is in the Docker image)")
            url = state.get("twitch_vod_url")
            if not url:
                raise SkipCheck("no Twitch VOD found")
            vod_id = url.rstrip("/").split("/")[-1].lstrip("v")
            out = tmp / "chat.json"
            subprocess.run([cli, "chatdownload", "--id", vod_id, "-b", "0", "-e", "600", "-o", str(out)],
                           check=True, capture_output=True, timeout=300)
            n = len(json.loads(out.read_text()).get("comments", []))
            return f"{n} chat messages in the first 10 minutes"
        results.append(_check("Twitch chat download", twitch_chat))

    return results
