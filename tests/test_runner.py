import json

from clipz.scout.rules_reader import RulesReader
from clipz.scout.runner import run_once
from clipz.scout.scorer import Scorer
from clipz.scout.sources.whop_bounties import bounty_to_campaign
from conftest import NOW, add_clip, load_fixture


class FixtureSource:
    name = "fixture"

    def __init__(self, campaigns):
        self.campaigns = campaigns

    def fetch(self):
        return self.campaigns


def campaigns_from_fixtures():
    raw = load_fixture("whop_bounties_page1.json")["data"] + load_fixture("whop_bounties_page2.json")["data"]
    return [bounty_to_campaign(b) for b in raw]


def test_end_to_end_offline(conn, scoring, watchlist):
    for i in range(3):
        add_clip(conn, f"clip{i}", "streamer_gta_one", length=30, score=0.9 - i / 10)
    source = FixtureSource(campaigns_from_fixtures())
    reader = RulesReader(conn)                       # no API key: keyword fallback
    scorer = Scorer(scoring, watchlist, "US")

    alerts = run_once(conn, [source], reader, scorer, now=NOW)
    assert [a["campaign_id"] for a in alerts] == ["whop_bounty:bnty_gta1"]
    assert alerts[0]["kind"] == "instant"
    assert alerts[0]["clips"] == ["clip0", "clip1", "clip2"]

    status = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM campaigns")}
    assert status == {
        "whop_bounty:bnty_gta1": "alerted",
        "whop_bounty:bnty_casino": "skipped",        # casino logo + promo code required
        "whop_bounty:bnty_drained": "skipped",       # 10% budget left
    }
    casino = conn.execute("SELECT reasons FROM scores WHERE campaign_id = 'whop_bounty:bnty_casino'").fetchone()
    assert any("platforms ban" in r for r in json.loads(casino["reasons"]))

    # Second poll: nothing new, no duplicate alerts.
    assert run_once(conn, [source], reader, scorer, now=NOW) == []


def test_budget_warning_for_joined_campaign(conn, scoring, watchlist):
    campaigns = campaigns_from_fixtures()[:1]
    source = FixtureSource(campaigns)
    reader, scorer = RulesReader(conn), Scorer(scoring, watchlist, "US")
    run_once(conn, [source], reader, scorer, now=NOW)
    conn.execute("UPDATE campaigns SET status = 'joined'")
    campaigns[0] = campaigns[0].model_copy(update={"budget_paid_out": 9000})
    alerts = run_once(conn, [FixtureSource(campaigns)], reader, scorer, now=NOW)
    assert alerts == [{"campaign_id": "whop_bounty:bnty_gta1", "kind": "budget_warning",
                       "title": campaigns[0].title}]
    assert conn.execute("SELECT status FROM campaigns").fetchone()["status"] == "drained"


def test_broken_source_does_not_stop_others(conn, scoring, watchlist):
    class Broken:
        name = "broken"

        def fetch(self):
            raise RuntimeError("site changed")

    ok = FixtureSource(campaigns_from_fixtures()[:1])
    alerts = run_once(conn, [Broken(), ok], RulesReader(conn), Scorer(scoring, watchlist, "US"), now=NOW)
    assert len(alerts) == 1
