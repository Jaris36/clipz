"""Phase 2: OpenShorts client, facecam crop, casino-text and music guards, render stage."""
import glob
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

from clipz.pipeline.config import ConfigError, PipelineSettings, _facecam_json, sync_streamers
from clipz.pipeline.render import facecam as fc
from clipz.pipeline.render.guards import (casino_text_hits, compile_banned, find_banned, is_music_heavy,
                                          ocr_frames)
from clipz.pipeline.render.openshorts import OpenShortsClient, OpenShortsError
from clipz.pipeline.render.stage import MAX_ATTEMPTS, pending, render_one
from clipz.pipeline.run import analyze, save_moments, upsert_vod
from clipz.pipeline.schema import connect
from clipz.settings import ROOT

RENDER_CFG = yaml.safe_load((ROOT / "config" / "render.yaml").read_text())
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
FONT = next(iter(glob.glob("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")), None)


def make_video(path: Path, seconds=3, size="640x360", text=None):
    vf = []
    if text and FONT:
        vf = ["-vf", f"drawtext=fontfile={FONT}:text='{text}':fontsize=48:fontcolor=white:"
                     "box=1:boxcolor=black:x=(w-text_w)/2:y=(h-text_h)/2"]
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=15",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds), *vf,
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True, capture_output=True)
    return path


# ---------- guards ----------

@pytest.mark.parametrize("text", ["USE CODE BIGWIN50 on Stake", "roobet.com", "Shuffle bonus", "free spins today",
                                  "code KICK100"])
def test_casino_text_is_caught(text):
    assert find_banned(text, compile_banned(RENDER_CFG), "hook")


@pytest.mark.parametrize("text", ["He shuffled the cards 😭", "NO WAY he hit that 🤯", "Chat lost it",
                                  "the encode broke"])
def test_ordinary_text_passes(text):
    assert find_banned(text, compile_banned(RENDER_CFG), "hook") == []


def test_casino_hits_cover_hook_caption_and_frames():
    hits = casino_text_hits(Path("x.mp4"), "MAX WIN 🤯", "go to rainbet now", RENDER_CFG,
                            ocr=lambda v: [(0.0, "nothing here"), (2.0, "STAKE.US promo")])
    assert {h.where for h in hits} == {"caption", "frame@2s"}


def test_music_flag():
    assert is_music_heavy(3, 30.0, -14.0, RENDER_CFG)          # loud, almost no words
    assert not is_music_heavy(60, 30.0, -14.0, RENDER_CFG)     # lots of talking
    assert not is_music_heavy(3, 30.0, -35.0, RENDER_CFG)      # quiet
    assert not is_music_heavy(3, 30.0, None, RENDER_CFG)


@needs_ffmpeg
@pytest.mark.skipif(not shutil.which("tesseract") or not FONT, reason="tesseract or font missing")
def test_ocr_reads_casino_name_off_the_screen(tmp_path):
    video = make_video(tmp_path / "v.mp4", seconds=3, size="1280x720", text="ROOBET.COM")
    frames = ocr_frames(video, every_seconds=1)
    assert any("ROOBET" in t.upper() for _, t in frames)
    assert casino_text_hits(video, "", "", RENDER_CFG)


# ---------- facecam ----------

def test_find_facecam_uses_the_steady_face():
    frames = list(range(12))
    detector = lambda f: [(1500, 60, 120, 120)] if f % 4 else [(1500, 60, 120, 120), (200, 500, 40, 40)]  # noqa
    box = fc.find_facecam(frames, 1920, 1080, padding=0.35, detector=detector)
    assert box.x <= 1500 and box.x + box.w >= 1620 and box.y <= 60 and box.y + box.h >= 180
    assert box.w % 2 == 0 and box.h % 2 == 0 and box.x + box.w <= 1920


def test_find_facecam_gives_up_without_a_face():
    with pytest.raises(fc.FacecamNotFound):
        fc.find_facecam(list(range(12)), 1920, 1080, detector=lambda f: [] if f else [(0, 0, 50, 50)])


def test_facecam_config_validation():
    assert json.loads(_facecam_json({"x": 0.7, "y": 0, "w": 0.3, "h": 0.3}))["w"] == 0.3
    assert _facecam_json(None) is None
    with pytest.raises(ConfigError):
        _facecam_json({"x": 0.8, "y": 0, "w": 0.4, "h": 0.3})


@needs_ffmpeg
def test_crop_to_configured_box(tmp_path):
    src = make_video(tmp_path / "s.mp4", size="1280x720")
    out, box = fc.facecam_only(src, tmp_path / "c.mp4", {"x": 0.75, "y": 0, "w": 0.25, "h": 0.33})
    w, h, dur = fc.probe_size(out)
    assert (w, h) == (box.w, box.h) == (320, 238) and dur > 2


# ---------- OpenShorts client ----------

def fake_openshorts(tmp_path, statuses=("queued", "processing", "completed"), clips=None, fail_submit=False):
    calls = {"status": 0, "submit": None}
    clips = clips if clips is not None else [{"video_url": "/videos/j1/clip_0.mp4", "start": 0.0, "end": 30.0,
                                              "viral_hook_text": "He did NOT see that coming 😳",
                                              "video_title_for_youtube_short": "Unreal ending"}]

    def handler(req: httpx.Request):
        if req.url.path == "/api/process":
            calls["submit"] = req.content
            if fail_submit:
                return httpx.Response(400, json={"detail": "Gemini key missing"})
            return httpx.Response(200, json={"job_id": "j1", "status": "queued"})
        if req.url.path == "/api/status/j1":
            st = statuses[min(calls["status"], len(statuses) - 1)]
            calls["status"] += 1
            return httpx.Response(200, json={"status": st, "logs": ["boom"],
                                             "result": {"clips": clips} if st == "completed" else None})
        if req.url.path.startswith("/videos/"):
            return httpx.Response(200, content=b"\x00" * 2048)
        if req.url.path == "/api/clip/j1/0/transcript":
            return httpx.Response(200, json={"words": [{"word": w} for w in "no way he hit that".split()]})
        return httpx.Response(404)

    client = OpenShortsClient("http://os:8000", "k", client=httpx.Client(transport=httpx.MockTransport(handler)),
                              poll_seconds=0)
    return client, calls


def test_client_submits_one_vertical_clip_and_waits(tmp_path):
    vid = tmp_path / "seg.mp4"
    vid.write_bytes(b"fake")
    client, calls = fake_openshorts(tmp_path)
    job = client.submit(vid)
    body = calls["submit"].decode(errors="ignore")
    for field, value in [("target_clips", "1"), ("output_format", "vertical"), ("acknowledged", "true"),
                         ("captions", "true")]:
        assert f'name="{field}"\r\n\r\n{value}' in body
    clips = client.wait(job)
    assert clips[0].hook.startswith("He did NOT") and calls["status"] == 3
    out = client.download(clips[0], tmp_path / "out.mp4")
    assert out.stat().st_size == 2048
    assert len(client.transcript_words(job)) == 5
    assert client.http.headers["X-Gemini-Key"] == "k"


def test_client_reports_failures(tmp_path):
    vid = tmp_path / "seg.mp4"
    vid.write_bytes(b"fake")
    with pytest.raises(OpenShortsError, match="Gemini"):
        fake_openshorts(tmp_path, fail_submit=True)[0].submit(vid)
    client, _ = fake_openshorts(tmp_path, statuses=("failed",))
    with pytest.raises(OpenShortsError, match="boom"):
        client.wait("j1")


# ---------- render stage ----------

@pytest.fixture
def setup(tmp_path):
    conn = connect(":memory:")
    settings = PipelineSettings(db_path=tmp_path / "db", data_dir=tmp_path / "data", render=RENDER_CFG)

    def add(name, niche, facecam=None):
        sync_streamers(conn, [{"name": name, "platform": "kick", "niche": niche, "facecam": facecam,
                               "permission": {"type": "campaign", "proof": "https://whop.com/x"}}])
        sid = conn.execute("SELECT id FROM streamers WHERE channel = ?", (name,)).fetchone()[0]
        vod_id, _ = upsert_vod(conn, sid, f"v-{name}", "t", f"https://kick.com/{name}/videos/1", "2026-11-27", 3600)
        save_moments(conn, vod_id, analyze(3600, {"min_score": 0.1, "moments_per_vod": 1}, None, [(600, 30, 100)]))
        return conn.execute("SELECT c.id FROM clips c JOIN moments m ON m.id = c.moment_id WHERE m.vod_id = ?",
                            (vod_id,)).fetchone()[0]

    def fetch(row, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        return make_video(out, size="1280x720")

    # Sync both streamers in one call so neither deactivates the other.
    yield conn, settings, add, fetch
    conn.close()


def row_for(conn, clip_id):
    return next(r for r in pending(conn, 50) if r["clip_id"] == clip_id)


@needs_ffmpeg
def test_gta_clip_renders(setup, tmp_path):
    conn, settings, add, fetch = setup
    cid = add("gta_guy", "gta")
    client, _ = fake_openshorts(tmp_path)
    out = render_one(conn, settings, row_for(conn, cid), client, fetch, loudness_fn=lambda p: -30.0)
    assert out.state == "rendered", out.detail
    c = conn.execute("SELECT * FROM clips WHERE id = ?", (cid,)).fetchone()
    assert c["hook_title"].startswith("He did NOT") and Path(c["file_path"]).exists()
    assert c["caption"] == "no way he hit that" and c["gambling_flag"] == 0 and c["length_seconds"] == 30.0
    assert not (settings.data_dir / "segments" / f"{cid}.mp4").exists()   # segment cleaned up


@needs_ffmpeg
def test_gambling_clip_with_casino_text_is_rejected(setup, tmp_path):
    conn, settings, add, fetch = setup
    cid = add("slots_guy", "kick_gambling", {"x": 0.75, "y": 0, "w": 0.25, "h": 0.33})
    client, calls = fake_openshorts(tmp_path)
    out = render_one(conn, settings, row_for(conn, cid), client, fetch,
                     ocr=lambda v: [(4.0, "USE CODE SLOTS on STAKE")], loudness_fn=lambda p: -30.0)
    assert out.state == "rejected" and "STAKE" in out.detail
    c = conn.execute("SELECT * FROM clips WHERE id = ?", (cid,)).fetchone()
    assert c["state"] == "rejected" and "casino text" in c["reject_reason"]
    assert not (settings.data_dir / "clips" / f"{cid}.mp4").exists()


@needs_ffmpeg
def test_gambling_clip_is_cropped_and_flagged(setup, tmp_path):
    conn, settings, add, fetch = setup
    cid = add("slots_guy", "kick_gambling", {"x": 0.75, "y": 0, "w": 0.25, "h": 0.33})
    seen = {}

    def spy_facecam(seg, out, box, n, pad):
        seen["box"] = box
        return fc.facecam_only(seg, out, box, n, pad)

    client, _ = fake_openshorts(tmp_path)
    out = render_one(conn, settings, row_for(conn, cid), client, fetch, facecam_fn=spy_facecam,
                     ocr=lambda v: [], loudness_fn=lambda p: -10.0)
    assert out.state == "rendered" and seen["box"]["w"] == 0.25
    c = conn.execute("SELECT * FROM clips WHERE id = ?", (cid,)).fetchone()
    assert c["gambling_flag"] == 1 and c["music_flag"] == 1   # 5 words in 30 s at -10 LUFS


@needs_ffmpeg
def test_no_facecam_rejects_gambling_clip(setup, tmp_path):
    conn, settings, add, fetch = setup
    cid = add("slots_guy", "kick_gambling")

    def no_face(*a):
        raise fc.FacecamNotFound("face found in only 0 of 12 frames")
    client, _ = fake_openshorts(tmp_path)
    out = render_one(conn, settings, row_for(conn, cid), client, fetch, facecam_fn=no_face)
    assert out.state == "rejected" and "watchlist.yaml" in out.detail


@needs_ffmpeg
def test_failures_retry_then_stop(setup, tmp_path):
    conn, settings, add, fetch = setup
    cid = add("gta_guy", "gta")
    client, _ = fake_openshorts(tmp_path, fail_submit=True)
    for _ in range(MAX_ATTEMPTS):
        out = render_one(conn, settings, row_for(conn, cid), client, fetch)
        assert out.state == "candidate" and "Gemini" in out.detail
    assert all(r["clip_id"] != cid for r in pending(conn, 50))
    assert "Gemini" in conn.execute("SELECT render_error FROM clips WHERE id = ?", (cid,)).fetchone()[0]
