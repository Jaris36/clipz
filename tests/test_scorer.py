from datetime import timedelta

from clipz.scout.models import Campaign, Rules
from clipz.scout.scorer import Scorer, match_streamer
from conftest import NOW


def gta_campaign(**kw):
    base = dict(id="whop_bounty:g", source="whop_bounty", source_id="g",
                title="GTA 6 clips - streamer_gta_one", description="Clip streamer_gta_one",
                pay_per_submission=25, budget_total=10000, budget_paid_out=1000,
                spots_remaining=200, unresolved_submissions=20, allowed_countries=["US"],
                created_at=NOW - timedelta(minutes=20))
    base.update(kw)
    return Campaign(**base)


def test_match_streamer_and_tier(watchlist):
    name, tier = match_streamer(gta_campaign(), Rules(), watchlist)
    assert (name, tier) == ("streamer_gta_one", 1)
    c = gta_campaign(title="Random GTA RP clips", description="any nopixel streamer")
    assert match_streamer(c, Rules(), watchlist) == (None, 1)        # niche keyword, no streamer
    c = gta_campaign(title="Cooking show", description="clip our show")
    assert match_streamer(c, Rules(), watchlist) == (None, 3)


def test_fresh_gta_campaign_scores_instant(scoring, watchlist):
    s = Scorer(scoring, watchlist, "US")
    r = s.score(gta_campaign(), Rules(platforms=["tiktok", "youtube"]), ready_clips=3, history=[], now=NOW)
    assert r.passed
    assert r.score >= 75, r.breakdown
    assert s.alert_kind(r.score, local_hour=15) == "instant"


def test_hard_filters(scoring, watchlist):
    s = Scorer(scoring, watchlist, "US")
    cases = {
        "budget": (gta_campaign(budget_paid_out=8000), Rules()),
        "country": (gta_campaign(allowed_countries=["GB"]), Rules()),
        "spots": (gta_campaign(spots_remaining=0), Rules()),
        "platform": (gta_campaign(), Rules(platforms=["x"])),
        "promo": (gta_campaign(), Rules(requires_promo_in_post=True, is_gambling=True)),
        "format": (gta_campaign(), Rules(requires_unsupported_format=True)),
        "closed": (gta_campaign(status_open=False), Rules()),
        "no source": (gta_campaign(title="Cooking", description="clip it"), Rules()),
    }
    for label, (c, rules) in cases.items():
        r = s.score(c, rules, ready_clips=3, history=[], now=NOW)
        assert not r.passed, label
        assert r.reasons, label


def test_supplied_footage_passes_without_streamer(scoring, watchlist):
    s = Scorer(scoring, watchlist, "US")
    c = gta_campaign(title="Podcast", description="footage provided")
    r = s.score(c, Rules(source_footage="drive link provided"), ready_clips=0, history=[], now=NOW)
    assert r.passed
    assert r.breakdown["niche_fit"] < 20


def test_pay_relative_to_history(scoring, watchlist):
    hist = [Campaign(id=f"h{i}", source="x", source_id=str(i), title="h", pay_per_submission=10) for i in range(5)]
    assert Scorer.pay_part(gta_campaign(pay_per_submission=20), hist) == 1.0
    assert Scorer.pay_part(gta_campaign(pay_per_submission=10), hist) == 0.5
    assert Scorer.pay_part(gta_campaign(pay_per_submission=10), hist[:2]) == 0.5   # too little history


def test_freshness_decays(scoring, watchlist):
    s = Scorer(scoring, watchlist, "US")
    assert s.freshness_part(gta_campaign(created_at=NOW - timedelta(minutes=30)), NOW) == 1.0
    mid = s.freshness_part(gta_campaign(created_at=NOW - timedelta(hours=24)), NOW)
    assert 0.4 < mid < 0.6
    assert s.freshness_part(gta_campaign(created_at=NOW - timedelta(hours=60)), NOW) == 0.0


def test_quiet_hours(scoring, watchlist):
    s = Scorer(scoring, watchlist, "US")
    assert s.alert_kind(80, local_hour=2) == "digest"
    assert s.alert_kind(95, local_hour=2) == "instant"
    assert s.alert_kind(60, local_hour=15) == "digest"
    assert s.alert_kind(40, local_hour=15) is None
