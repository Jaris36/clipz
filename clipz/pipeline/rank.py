"""Merge per-window signals into a ranked list of moments."""
from __future__ import annotations

from dataclasses import dataclass, field

from clipz.pipeline.signals.chat_spikes import ChatWindows, dominant_category


@dataclass
class Moment:
    start_s: float
    end_s: float
    peak_s: float
    score: float
    parts: dict[str, float]
    moment_type: str = "mixed"
    sample_chat: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def combine(signals: dict[str, list[float]], weights: dict[str, float]) -> list[float]:
    n = max((len(v) for v in signals.values()), default=0)
    total = [0.0] * n
    for name, values in signals.items():
        w = weights.get(name, 0.0)
        for i, v in enumerate(values):
            total[i] += w * v
    # Light smoothing so a strong moment spanning two windows beats a one-window blip.
    return [
        0.25 * total[i - 1] + 0.5 * total[i] + 0.25 * total[i + 1] if 0 < i < n - 1 else total[i]
        for i in range(n)
    ]


def pick_moments(total: list[float], signals: dict[str, list[float]], weights: dict[str, float],
                 window_s: float, duration_s: float, cfg: dict,
                 chat: ChatWindows | None = None) -> list[Moment]:
    k = int(cfg.get("moments_per_vod", 15))
    min_gap_w = max(1, int(cfg.get("min_gap_seconds", 60) // window_s))
    max_core_w = max(1, int(cfg.get("max_core_seconds", 60) // window_s))
    pad = float(cfg.get("padding_seconds", 20))
    min_score = float(cfg.get("min_score", 1.0))

    order = sorted(range(len(total)), key=lambda i: total[i], reverse=True)
    taken: list[int] = []
    moments: list[Moment] = []
    for i in order:
        if len(moments) >= k or total[i] < min_score:
            break
        if any(abs(i - j) < min_gap_w for j in taken):
            continue
        # Grow the core while neighbours stay above half the peak.
        lo = hi = i
        half = total[i] * 0.5
        while lo > 0 and total[lo - 1] >= half and (hi - lo + 1) < max_core_w:
            lo -= 1
        while hi < len(total) - 1 and total[hi + 1] >= half and (hi - lo + 1) < max_core_w:
            hi += 1
        taken.append(i)
        start = max(0.0, lo * window_s - pad)
        end = min(duration_s, (hi + 1) * window_s + pad)
        parts = {name: round(weights.get(name, 0) * vals[i], 3) for name, vals in signals.items() if i < len(vals)}
        mtype = dominant_category(chat, i) if chat else "mixed"
        if parts.get("viewer_clips", 0) > 0 and mtype == "mixed":
            mtype = "viewer_clip"
        sample = []
        if chat:
            for j in range(max(0, i - 1), min(chat.n, i + 2)):
                sample.extend(chat.samples[j])
        moments.append(Moment(start, end, i * window_s + window_s / 2, round(total[i], 3),
                              parts, mtype, sample[:8]))
    return moments
