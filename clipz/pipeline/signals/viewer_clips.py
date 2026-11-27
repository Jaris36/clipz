"""Viewer-made clips: the strongest free signal. A window covered by a clip that
many people watched is a proven moment."""
from __future__ import annotations

import math


def viewer_clip_score(clips: list[tuple[float, float, int]], n_windows: int, window_s: float,
                      top: float = 3.0) -> list[float]:
    """clips = [(vod_offset_s, duration_s, view_count)]. Scaled so the best window = `top`."""
    scores = [0.0] * n_windows
    for offset, duration, views in clips:
        if offset is None:
            continue
        first = int(max(0, offset) // window_s)
        last = int(max(0, offset + duration - 1e-6) // window_s)
        weight = math.log1p(max(0, views)) + 1.0  # even a 0-view clip means someone pressed Clip
        for i in range(first, min(last + 1, n_windows)):
            scores[i] += weight
    peak = max(scores) if scores else 0
    return [s / peak * top for s in scores] if peak > 0 else scores
