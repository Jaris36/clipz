"""Chat-spike scoring: message rate and reaction tokens per window.

Chat reacts a few seconds after a moment, so window times are shifted back by
chat_lag_seconds before they are compared with video time.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from clipz.pipeline.signals import robust_z, squash

CATEGORIES = ("funny", "hype", "shock", "clip")


@dataclass
class ChatWindows:
    window_s: float
    counts: list[int]
    category_counts: dict[str, list[int]]
    samples: list[list[str]] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.counts)


def _compile(tokens: dict[str, list[str]]) -> dict[str, re.Pattern]:
    pats = {}
    for cat in CATEGORIES:
        words = [t.strip() for t in tokens.get(cat, []) if t.strip()]
        if not words:
            continue
        parts = []
        for w in sorted(words, key=len, reverse=True):
            esc = re.escape(w)
            # Whole-word match for plain words; substring for emoji and punctuation runs.
            parts.append(rf"(?<!\w){esc}(?!\w)" if re.fullmatch(r"[\w' ]+", w) else esc)
        pats[cat] = re.compile("|".join(parts), re.IGNORECASE)
    return pats


def bucket_chat(rows: list[tuple[float, str]], duration_s: float, window_s: float,
                lag_s: float, tokens: dict[str, list[str]], samples_per_window: int = 5) -> ChatWindows:
    n = max(1, math.ceil(duration_s / window_s))
    counts = [0] * n
    cats = {c: [0] * n for c in CATEGORIES}
    samples: list[list[str]] = [[] for _ in range(n)]
    pats = _compile(tokens)
    for offset, text in rows:
        t = offset - lag_s
        if t < 0 or t >= duration_s:
            continue
        i = int(t // window_s)
        counts[i] += 1
        for cat, pat in pats.items():
            if pat.search(text):
                cats[cat][i] += 1
        if len(samples[i]) < samples_per_window and text:
            samples[i].append(text[:120])
    return ChatWindows(window_s, counts, cats, samples)


def chat_rate_score(w: ChatWindows) -> list[float]:
    return squash(robust_z([float(c) for c in w.counts]))


def emote_score(w: ChatWindows) -> list[float]:
    """Spike in reaction messages (laughs, hype, shock, clip requests)."""
    reacting = [sum(w.category_counts[c][i] for c in CATEGORIES) for i in range(w.n)]
    return squash(robust_z([float(r) for r in reacting]))


def dominant_category(w: ChatWindows, i: int, radius: int = 1) -> str:
    lo, hi = max(0, i - radius), min(w.n, i + radius + 1)
    totals = {c: sum(w.category_counts[c][lo:hi]) for c in CATEGORIES}
    best = max(totals, key=totals.get)
    if totals[best] == 0:
        return "mixed"
    return "clip_request" if best == "clip" else best
