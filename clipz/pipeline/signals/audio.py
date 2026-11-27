"""Audio loudness spikes (screams, laughter, big reactions) via ffmpeg's EBU R128 meter."""
from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path

from clipz.pipeline.signals import robust_z, squash

_LINE = re.compile(r"t:\s*([\d.]+)\s+.*?M:\s*(-?[\d.]+|-inf)")


def momentary_loudness(audio_path: Path, timeout: int = 3600) -> list[tuple[float, float]]:
    """[(time_s, momentary LUFS)] about every 0.1 s."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(audio_path), "-vn",
         "-af", "ebur128=framelog=verbose", "-f", "null", "-"],
        capture_output=True, text=True, timeout=timeout,
    )
    return parse_ebur128(proc.stderr)


def parse_ebur128(stderr: str) -> list[tuple[float, float]]:
    out = []
    for m in _LINE.finditer(stderr):
        val = m.group(2)
        out.append((float(m.group(1)), -120.0 if val == "-inf" else float(val)))
    return out


def audio_score(samples: list[tuple[float, float]], n_windows: int, window_s: float) -> list[float]:
    peaks = [-120.0] * n_windows
    for t, lufs in samples:
        i = int(t // window_s)
        if 0 <= i < n_windows:
            peaks[i] = max(peaks[i], lufs)
    # Ignore silence when judging what "typical" is.
    floor = min((p for p in peaks if p > -70), default=-70.0)
    return squash(robust_z([max(p, floor) for p in peaks])) if not math.isinf(floor) else [0.0] * n_windows
