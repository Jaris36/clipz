"""Whop official API: GET /bounties (read-only).

Docs: https://docs.whop.com/api-reference/beta/bounties/list-bounties.md
Needs a user OAuth token with the bounty:basic:read scope.

Open question: whether Content Rewards campaigns show up here. Test with Preston's
own account in Phase 3 before relying on it.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import httpx

from clipz.scout.models import Campaign

API_BASE = "https://api.whop.com/api/v1"
MAX_PAGES = 5            # 5 x 100 newest open clipping bounties is plenty per poll


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def bounty_to_campaign(b: dict) -> Campaign:
    """Map one Whop bounty object onto our Campaign shape.

    Assumption to verify in Phase 3: amount fields are in currency units (dollars), not cents.
    """
    bounty_id = str(b["id"])
    return Campaign(
        id=f"whop_bounty:{bounty_id}",
        source="whop_bounty",
        source_id=bounty_id,
        title=b.get("title") or bounty_id,
        description=b.get("description") or "",
        link=f"https://whop.com/bounties/{bounty_id}" if bounty_id else None,
        status_open=(b.get("status") == "open"),
        # The quoted, after-fee reward a worker gets per accepted submission.
        pay_per_submission=_to_float(b.get("net_reward_amount") or b.get("gross_reward_amount")),
        currency=(b.get("currency") or "usd").lower(),
        budget_total=_to_float(b.get("budget_amount")),
        budget_paid_out=_to_float(b.get("gross_paid_out_amount")),
        spots_remaining=b.get("spots_remaining"),
        unresolved_submissions=b.get("unresolved_submissions_count"),
        allowed_countries=[c.upper() for c in (b.get("allowed_country_codes") or [])],
        created_at=_to_dt(b.get("created_at")),
        deadline=_to_dt(b.get("submissions_closed_at")),
    )


class WhopBountiesSource:
    name = "whop_bounty"

    def __init__(self, token: str, client: httpx.Client | None = None, country: str | None = None):
        self._client = client or httpx.Client(base_url=API_BASE, timeout=20.0)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._country = country

    def _get(self, params: dict) -> dict:
        for attempt in range(4):
            resp = self._client.get("/bounties", params=params, headers=self._headers)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def fetch(self) -> list[Campaign]:
        params: dict[str, Any] = {
            "status": "open",
            "business_goal_type": "clipping",
            "order": "created_at",
            "direction": "desc",
            "first": 100,
        }
        if self._country:
            params["country"] = self._country
        campaigns: list[Campaign] = []
        for _ in range(MAX_PAGES):
            page = self._get(params)
            campaigns.extend(bounty_to_campaign(b) for b in page.get("data", []))
            info = page.get("page_info") or {}
            if not info.get("has_next_page"):
                break
            params["after"] = info.get("end_cursor")
        return campaigns
