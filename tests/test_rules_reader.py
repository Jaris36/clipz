from types import SimpleNamespace

from clipz.scout.models import Campaign, Rules
from clipz.scout.rules_reader import RulesReader, keyword_rules
from clipz.scout.runner import upsert_campaign


def test_keyword_fallback_catches_casino_promo():
    r = keyword_rules("Clip slots streams. Show the casino logo and use code WIN100. TikTok only.")
    assert r.is_gambling
    assert r.requires_promo_in_post
    assert r.platforms == ["tiktok"]


def test_keyword_fallback_reads_tags():
    r = keyword_rules("Tag @streamer_gta_one, use #gta6 #ad. YouTube Shorts and TikTok.")
    assert r.required_tags == ["@streamer_gta_one"]
    assert r.required_hashtags == ["#ad", "#gta6"]
    assert set(r.platforms) == {"tiktok", "youtube"}


class FakeClient:
    def __init__(self, rules):
        self.calls = 0
        self.messages = SimpleNamespace(parse=self._parse)
        self._rules = rules

    def _parse(self, **kwargs):
        self.calls += 1
        assert kwargs["model"] == "claude-haiku-5-5"
        assert kwargs["output_format"] is Rules
        return SimpleNamespace(stop_reason="end_turn", parsed_output=self._rules)


def test_model_result_is_cached(conn):
    c = Campaign(id="manual:x", source="manual", source_id="x", title="t", description="d")
    upsert_campaign(conn, c)
    fake = FakeClient(Rules(platforms=["tiktok"], min_length_seconds=15))
    reader = RulesReader(conn, client=fake)
    assert reader.read(c).min_length_seconds == 15
    assert reader.read(c).min_length_seconds == 15
    assert fake.calls == 1                         # second read came from the cache
    c2 = c.model_copy(update={"description": "changed brief"})
    reader.read(c2)
    assert fake.calls == 2                         # changed brief is re-read


def test_refusal_falls_back_to_keywords(conn):
    c = Campaign(id="manual:y", source="manual", source_id="y", title="t", description="TikTok only")
    upsert_campaign(conn, c)
    fake = FakeClient(None)
    fake.messages = SimpleNamespace(parse=lambda **k: SimpleNamespace(stop_reason="refusal", parsed_output=None))
    r = RulesReader(conn, client=fake).read(c)
    assert r.platforms == ["tiktok"]
    assert r.unclear_fields
