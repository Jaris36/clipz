"""Per-window highlight signals. Each returns a list of floats, one per window."""
from __future__ import annotations

import math
import statistics


def robust_z(values: list[float]) -> list[float]:
    """How far each value sits above the typical level, in robust standard deviations.
    Uses median and MAD so a few huge spikes don't hide the rest. Negative -> 0."""
    if not values:
        return []
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    scale = 1.4826 * mad
    if scale < 1e-9:
        # Flat signal (or mostly zeros): fall back to mean absolute deviation.
        mean = statistics.fmean(values)
        scale = statistics.fmean(abs(v - mean) for v in values) or 1.0
    return [max(0.0, (v - med) / scale) for v in values]


def squash(values: list[float], cap: float = 3.0, softness: float = 4.0) -> list[float]:
    """Soft-cap z-scores at `cap` so one signal can't drown out the others.
    z=2 -> 1.4, z=4 -> 2.3, z=35 -> 3.0. Viewer clips use the same 0-3 scale."""
    return [cap * math.tanh(v / softness) for v in values]
