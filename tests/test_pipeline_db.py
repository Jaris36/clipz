"""Pipeline tables extend the scout's shared schema; the scout can read banked clips."""
import textwrap
from datetime import datetime, timezone

import pytest

from clipz.pipeline.config import ConfigError, load_streamers, sync_streamers
from clipz.pipeline.run import analyze, save_moments, upsert_vod
from clipz.pipeline.schema import CLIP_COLUMNS, connect
from clipz.scout.matcher import banked_clips


@pytest.fixture
def pconn():
    c = connect(":memory:")
    yield c
    c.close()


def add_streamer(conn, name="kick_slots_guy", platform="kick", niche="kick_gambling"):
    sync_streamers(conn, [{"name": name, "platform": platform, "niche": niche,
                           "permission": {"type": "campaign", "proof": "https://whop.com/x"}}])
    return conn.execute("SELECT id FROM streamers WHERE channel = ?", (name,)).fetchone()["id"]


def test_extends_scout_clips_table_without_redefining(pconn):
    cols = {r[1] for r in pconn.execute("PRAGMA table_info(clips)")}
    assert {"id", "streamer", "state", "length_seconds", "moment_score", "hook_title", "file_path"} <= cols
    assert set(CLIP_COLUMNS) <= cols
    # Running ensure twice is harmless.
    from clipz.pipeline.schema import ensure
    ensure(pconn)


def test_save_moments_creates_candidate_clips(pconn):
    sid = add_streamer(pconn)
    vod_id, new = upsert_vod(pconn, sid, "abc", "Slots", "https://kick.com/x/videos/abc", None, 3600)
    assert new and upsert_vod(pconn, sid, "abc", "Slots", "u", None, 3600) == (vod_id, False)
    moments = analyze(3600, {"moments_per_vod": 3, "min_score": 0.1}, None, [(600, 30, 100), (1500, 30, 10)])
    save_moments(pconn, vod_id, moments)
    rows = pconn.execute("SELECT * FROM clips ORDER BY moment_score DESC").fetchall()
    assert len(rows) == len(moments) == 2
    assert rows[0]["state"] == "candidate" and rows[0]["streamer"] == "kick_slots_guy"
    assert rows[0]["niche"] == "kick_gambling" and rows[0]["length_seconds"] > 0
    # Re-ranking replaces candidates instead of piling them up.
    save_moments(pconn, vod_id, moments)
    assert pconn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 2
    assert pconn.execute("SELECT status FROM vods WHERE id = ?", (vod_id,)).fetchone()[0] == "ranked"


def test_scout_sees_only_banked_clips(pconn):
    sid = add_streamer(pconn)
    vod_id, _ = upsert_vod(pconn, sid, "v1", "t", "u", None, 3600)
    save_moments(pconn, vod_id, analyze(3600, {"min_score": 0.1}, None, [(600, 30, 100), (1500, 30, 10)]))
    assert banked_clips(pconn, "kick_slots_guy") == []
    pconn.execute("UPDATE clips SET state = 'banked', created_at = '2026-11-17T12:00:00' WHERE moment_score = "
                  "(SELECT MAX(moment_score) FROM clips)")
    found = banked_clips(pconn, "kick_slots_guy", now=datetime(2026, 11, 18, 20, tzinfo=timezone.utc))
    assert len(found) == 1


def test_load_streamers_requires_permission(tmp_path):
    p = tmp_path / "watchlist.yaml"
    p.write_text(textwrap.dedent("""
        streamers:
          - name: ok_streamer
            platform: twitch
            niche: gta
            permission: {type: clipper_program, proof: "https://discord.gg/x"}
          - name: no_permission
            platform: kick
            niche: kick_gambling
          - name: yt_only
            platform: youtube
            niche: gta
            permission: {type: campaign, proof: "https://whop.com/y"}
    """))
    ok, skipped = load_streamers(p)
    assert [s["name"] for s in ok] == ["ok_streamer"]
    assert len(skipped) == 2 and any("no permission" in r for r in skipped)
    with pytest.raises(ConfigError):
        load_streamers(tmp_path / "missing.yaml")


def test_unlisted_streamers_are_deactivated(pconn):
    add_streamer(pconn, "a")
    add_streamer(pconn, "b")  # sync with only b -> a deactivated
    active = [r[0] for r in pconn.execute("SELECT channel FROM streamers WHERE active = 1")]
    assert active == ["b"]
