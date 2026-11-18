import httpx

from clipz.scout.sources.whop_bounties import WhopBountiesSource, bounty_to_campaign
from conftest import load_fixture


def test_bounty_mapping():
    b = load_fixture("whop_bounties_page1.json")["data"][0]
    c = bounty_to_campaign(b)
    assert c.id == "whop_bounty:bnty_gta1"
    assert c.pay_per_submission == 22.5            # net (after-fee) reward
    assert c.budget_left == 9000
    assert round(c.budget_left_pct) == 90
    assert c.allowed_countries == ["US", "CA"]
    assert c.currency == "usd"


def test_pagination_and_auth_and_filters():
    pages = {None: "whop_bounties_page1.json", "cur1": "whop_bounties_page2.json"}
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.url.params["status"] == "open"
        assert request.url.params["business_goal_type"] == "clipping"
        return httpx.Response(200, json=load_fixture(pages[request.url.params.get("after")]))

    client = httpx.Client(base_url="https://api.whop.com/api/v1", transport=httpx.MockTransport(handler))
    campaigns = WhopBountiesSource("test-token", client=client).fetch()
    assert [c.source_id for c in campaigns] == ["bnty_gta1", "bnty_casino", "bnty_drained"]
    assert len(seen) == 2


def test_retries_on_429(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"data": [], "page_info": {"has_next_page": False}})

    client = httpx.Client(base_url="https://api.whop.com/api/v1", transport=httpx.MockTransport(handler))
    assert WhopBountiesSource("t", client=client).fetch() == []
    assert calls["n"] == 2
