"""Moment finder: chat spikes, viewer clips, audio and ranking."""
import json
import random

import yaml

from clipz.pipeline.fetch.chat import load_twitch_chat
from clipz.pipeline.run import analyze, moments_report
from clipz.pipeline.signals import robust_z
from clipz.pipeline.signals.audio import audio_score, parse_ebur128
from clipz.pipeline.signals.chat_spikes import bucket_chat, dominant_category
from clipz.pipeline.signals.viewer_clips import viewer_clip_score
from clipz.settings import ROOT

CFG = yaml.safe_load((ROOT / "config" / "moment_scoring.yaml").read_text())
HOUR = 3600


def synthetic_chat(seed=7):
    """One hour of quiet chat with three planted moments. Chat reacts ~8 s late."""
    rng = random.Random(seed)
    rows = [(t + rng.random() * 5, rng.choice(["hi", "what game is this", "lol", "gg", "true"]))
            for t in range(0, HOUR, 5)]
    rows += [(608 + i * 0.3, "KEKW " * (1 + i % 3)) for i in range(60)]          # funny at ~600 s
    rows += [(1808 + i * 0.3, "POGGERS NO WAY") for i in range(45)]              # hype at ~1800 s
    rows += [(3008 + i * 0.5, "clip it") for i in range(20)]                     # clip request at ~3000 s
    return sorted(rows)


def test_robust_z_ignores_flat_baseline():
    z = robust_z([2, 2, 3, 2, 2, 40, 2, 3])
    assert z.index(max(z)) == 5
    assert all(v >= 0 for v in z)


def test_chat_lag_moves_spike_back_to_the_moment():
    w = bucket_chat(synthetic_chat(), HOUR, 10, 8, CFG["tokens"])
    peak = max(range(w.n), key=lambda i: w.counts[i])
    assert peak * 10 in (600, 610)
    assert dominant_category(w, 60) == "funny"
    assert dominant_category(w, 180) == "hype"
    assert dominant_category(w, 300) == "clip_request"


def test_planted_moments_rank_first():
    moments = analyze(HOUR, CFG, synthetic_chat())
    peaks = sorted(round(m.peak_s, -2) for m in moments[:3])
    assert peaks == [600, 1800, 3000]
    top = moments[0]
    assert top.start_s <= 600 <= top.end_s
    assert top.duration_s <= CFG["max_core_seconds"] + 2 * CFG["padding_seconds"]
    assert top.sample_chat
    assert "KEKW" in moments_report(moments)


def test_moments_respect_min_gap_and_limit():
    moments = analyze(HOUR, CFG, synthetic_chat())
    assert len(moments) <= CFG["moments_per_vod"]
    peaks = sorted(m.peak_s for m in moments)
    assert all(b - a >= CFG["min_gap_seconds"] for a, b in zip(peaks, peaks[1:]))


def test_viewer_clip_lifts_a_moment_chat_missed():
    clips = [(2400.0, 30.0, 5000)]
    moments = analyze(HOUR, CFG, synthetic_chat(), clips)
    assert any(2400 <= m.peak_s <= 2430 for m in moments[:4])
    without = analyze(HOUR, CFG, synthetic_chat())
    assert not any(2400 <= m.peak_s <= 2430 for m in without[:4])


def test_viewer_clip_score_scaling():
    s = viewer_clip_score([(100, 30, 10), (500, 20, 10000), (None, 30, 50)], 100, 10)
    assert max(s) == 3.0
    assert s.index(3.0) == 50
    assert s[10] > 0 and s[0] == 0


def test_clips_only_vod_still_ranks():
    moments = analyze(HOUR, CFG, None, [(1200, 30, 300), (2000, 25, 40)])
    assert moments and moments[0].peak_s // 100 == 12
    assert moments[0].moment_type == "viewer_clip"


def test_no_signals_returns_nothing():
    assert analyze(HOUR, CFG) == []


def test_audio_parse_and_score():
    lines = "\n".join(
        f"[Parsed_ebur128_0 @ 0x1] t: {t / 10:.1f}  TARGET:-23 LUFS    M: {(-8.0 if 300 <= t < 320 else -30.0):.1f} S: -30.0"
        for t in range(0, 600))
    lines += "\n[Parsed_ebur128_0 @ 0x1] t: 60.1  TARGET:-23 LUFS    M: -inf S: -inf"
    samples = parse_ebur128(lines)
    assert samples[0] == (0.0, -30.0)
    assert samples[-1] == (60.1, -120.0)
    score = audio_score(samples, 7, 10)
    assert score.index(max(score)) == 3


def test_load_twitch_chat(tmp_path):
    doc = {"comments": [
        {"content_offset_seconds": 12.5, "message": {"body": "KEKW"}},
        {"content_offset_seconds": 3.0, "message": {"fragments": [{"text": "hello "}, {"text": "LUL"}]}},
        {"message": {"body": "no offset, dropped"}},
    ]}
    p = tmp_path / "chat.json"
    p.write_text(json.dumps(doc))
    assert load_twitch_chat(p) == [(3.0, "hello LUL"), (12.5, "KEKW")]
