"""Download only the parts of a VOD we need, never the whole stream."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def hms(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def ytdlp_cmd() -> list[str]:
    exe = shutil.which("yt-dlp")
    return [exe] if exe else [sys.executable, "-m", "yt_dlp"]


def download_segment(url: str, start_s: float, end_s: float, out_path: Path,
                     max_height: int = 1080, proxy: str | None = None, timeout: int = 1800) -> Path:
    """Download [start_s, end_s] of a VOD with yt-dlp (works for Twitch and Kick)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ytdlp_cmd() + [
        "--quiet", "--no-progress", "--no-playlist",
        "-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]/best",
        "--download-sections", f"*{hms(start_s)}-{hms(end_s)}",
        "--merge-output-format", "mp4",
        "-o", str(out_path), "--force-overwrites",
    ]
    if proxy:
        cmd += ["--proxy", proxy]
    subprocess.run(cmd + [url], check=True, timeout=timeout, capture_output=True)
    return out_path


def download_twitch_segment(vod_id: str, start_s: float, end_s: float, out_path: Path,
                            cli: str = "TwitchDownloaderCLI", quality: str = "1080p60",
                            timeout: int = 1800) -> Path:
    """Same as download_segment, through TwitchDownloaderCLI (falls back to yt-dlp)."""
    if not shutil.which(cli):
        return download_segment(f"https://www.twitch.tv/videos/{vod_id}", start_s, end_s, out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [cli, "videodownload", "--id", vod_id, "-b", str(int(start_s)), "-e", str(int(end_s)),
         "-q", quality, "-o", str(out_path), "--collision", "Overwrite"],
        check=True, timeout=timeout, capture_output=True,
    )
    return out_path


def download_audio(url: str, out_path: Path, proxy: str | None = None, timeout: int = 3600) -> Path:
    """Audio-only track for loudness scoring (Twitch offers one; ~70 MB per hour)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ytdlp_cmd() + ["--quiet", "--no-progress", "-f", "audio_only/bestaudio/worst",
                         "-x", "--audio-format", "m4a", "-o", str(out_path), "--force-overwrites"]
    if proxy:
        cmd += ["--proxy", proxy]
    subprocess.run(cmd + [url], check=True, timeout=timeout, capture_output=True)
    return out_path
