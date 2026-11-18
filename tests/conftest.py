import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from clipz import db
from clipz.settings import ROOT

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)   # 3 PM New York


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def scoring():
    return yaml.safe_load((ROOT / "config" / "scoring.yaml").read_text())


@pytest.fixture
def watchlist():
    wl = yaml.safe_load((ROOT / "config" / "watchlist.yaml").read_text())
    wl["streamers"] = [
        {"name": "streamer_gta_one", "platform": "twitch", "niche": "gta"},
        {"name": "kick_slots_guy", "platform": "kick", "niche": "kick_gambling"},
    ]
    return wl


def add_clip(conn, cid, streamer, length=30, score=0.8, state="banked", created="2026-11-17T12:00:00"):
    conn.execute(
        "INSERT INTO clips (id, streamer, state, length_seconds, moment_score, hook_title, created_at) "
        "VALUES (?,?,?,?,?,?,?)", (cid, streamer, state, length, score, f"hook {cid}", created))
    conn.commit()
