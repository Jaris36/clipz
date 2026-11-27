"""Twitch chat download (TwitchDownloaderCLI) and loading into (offset, text) rows."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def download_twitch_chat(vod_id: str, out_path: Path, cli: str = "TwitchDownloaderCLI",
                         timeout: int = 3600) -> Path:
    """Full chat log for a Twitch VOD as TwitchDownloader JSON (small: a few MB per stream)."""
    if not shutil.which(cli):
        raise FileNotFoundError(f"{cli} not found; it is installed in the Docker image")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [cli, "chatdownload", "--id", vod_id, "-o", str(out_path), "--collision", "Overwrite"],
        check=True, timeout=timeout, capture_output=True,
    )
    return out_path


def load_twitch_chat(path: Path) -> list[tuple[float, str]]:
    """TwitchDownloader chat JSON -> [(offset_seconds, message_text)] sorted by time."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = []
    for c in doc.get("comments", []):
        offset = c.get("content_offset_seconds")
        msg = c.get("message") or {}
        text = msg.get("body")
        if text is None:
            text = "".join(f.get("text", "") for f in msg.get("fragments") or [])
        if offset is not None:
            rows.append((float(offset), text or ""))
    rows.sort(key=lambda r: r[0])
    return rows
