"""Checks a rendered clip must pass before review.

* Casino text: no casino name, web address or promo code on screen, in the hook
  or in the captions of a gambling clip. OCR uses the tesseract command.
* Music: mostly loud audio with little speech is likely background music that
  Content ID will claim, so the clip is flagged for Preston to mute or skip.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TextHit:
    where: str      # hook | caption | frame@12.0s
    term: str
    text: str


def compile_banned(cfg: dict) -> list[tuple[str, re.Pattern]]:
    banned = cfg.get("banned_text") or {}
    out = []
    for word in banned.get("casinos") or []:
        out.append((word, re.compile(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", re.I)))
    for phrase in banned.get("phrases") or []:
        out.append((phrase, re.compile(re.escape(phrase), re.I)))
    for pat in banned.get("patterns") or []:
        out.append((pat, re.compile(pat, re.I)))
    return out


def find_banned(text: str, banned: list[tuple[str, re.Pattern]], where: str) -> list[TextHit]:
    hits = []
    for term, pat in banned:
        m = pat.search(text or "")
        if m:
            hits.append(TextHit(where, term, m.group(0)))
    return hits


def _ocr_image(path: Path) -> str:
    return subprocess.run(["tesseract", str(path), "-", "--psm", "11"],
                          capture_output=True, text=True, timeout=120).stdout


def _text_layers(img_path: Path) -> list[Path]:
    """Stream overlays put white or black text on busy, colourful video, which
    plain OCR misses. Besides the frame itself, OCR two masks: near-white
    unsaturated pixels and near-black pixels, each drawn black on white, 2x size."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return [img_path]
    img = cv2.imread(str(img_path))
    if img is None:
        return [img_path]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    sat, val = hsv[..., 1], hsv[..., 2]
    layers = [img_path]
    for name, mask in (("white", (val > 190) & (sat < 60)), ("black", val < 60)):
        out = np.where(mask, 0, 255).astype("uint8")
        out = cv2.resize(out, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
        p = img_path.with_name(f"{img_path.stem}-{name}.png")
        cv2.imwrite(str(p), out)
        layers.append(p)
    return layers


def ocr_frames(video: Path, every_seconds: float = 2.0, max_frames: int = 30) -> list[tuple[float, str]]:
    """Text read from a frame every couple of seconds. Empty if tesseract is missing."""
    if not shutil.which("tesseract"):
        return []
    out = []
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", str(video), "-vf", f"fps=1/{every_seconds}",
             "-frames:v", str(max_frames), str(Path(d) / "f%04d.png")],
            check=True, capture_output=True, timeout=600)
        for i, img in enumerate(sorted(Path(d).glob("f[0-9][0-9][0-9][0-9].png"))):
            text = " ".join(_ocr_image(layer) for layer in _text_layers(img))
            out.append((i * every_seconds, " ".join(text.split())))
    return out


def casino_text_hits(video: Path | None, hook: str, caption: str, cfg: dict,
                     ocr=ocr_frames) -> list[TextHit]:
    banned = compile_banned(cfg)
    hits = find_banned(hook, banned, "hook") + find_banned(caption, banned, "caption")
    if video is not None:
        for t, text in ocr(video):
            hits += find_banned(text, banned, f"frame@{t:.0f}s")
    return hits


def is_music_heavy(words: list[dict] | int, duration_s: float, mean_lufs: float | None, cfg: dict) -> bool:
    """True when the clip is loud but nearly wordless. words = word list or word count."""
    music = cfg.get("music") or {}
    if duration_s <= 0 or mean_lufs is None:
        return False
    n_words = words if isinstance(words, int) else len(words)
    wps = n_words / duration_s
    return wps < float(music.get("max_words_per_second", 0.6)) and mean_lufs > float(music.get("min_mean_lufs", -24))


def integrated_loudness(video: Path) -> float | None:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-vn", "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True, timeout=600)
    m = re.findall(r"I:\s*(-?[\d.]+) LUFS", proc.stderr)
    return float(m[-1]) if m else None
