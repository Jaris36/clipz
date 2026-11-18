from clipz.scout.matcher import pick_clips
from clipz.scout.models import Rules
from conftest import NOW, add_clip


def test_picks_top_banked_clips_within_rules(conn):
    add_clip(conn, "a", "streamer_gta_one", length=30, score=0.9)
    add_clip(conn, "b", "streamer_gta_one", length=90, score=0.95)            # too long
    add_clip(conn, "c", "streamer_gta_one", length=20, score=0.7)
    add_clip(conn, "d", "streamer_gta_one", length=25, score=0.99, state="posted")   # not banked
    add_clip(conn, "e", "streamer_gta_one", length=25, score=0.99, created="2026-10-01T00:00:00")  # stale
    add_clip(conn, "f", "Streamer_GTA_One", length=40, score=0.6)
    add_clip(conn, "g", "streamer_gta_one", length=40, score=0.5)
    clips = pick_clips(conn, "streamer_gta_one", Rules(max_length_seconds=60), now=NOW)
    assert [c.id for c in clips] == ["a", "c", "f"]


def test_no_streamer_no_clips(conn):
    assert pick_clips(conn, None, Rules(), now=NOW) == []
