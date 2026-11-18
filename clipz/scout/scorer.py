"""Hard filters plus a 0-100 score. Weights and thresholds come from config/scoring.yaml."""
from __future__ import annotations

import statistics
from datetime import datetime, timezone

from clipz.scout.models import Campaign, Rules, ScoreResult


def match_streamer(campaign: Campaign, rules: Rules, watchlist: dict) -> tuple[str | None, int]:
    """Return (watchlist streamer name or None, tier)."""
    text = f"{campaign.title}\n{campaign.description}".lower()
    names_in_rules = {n.lower() for n in rules.streamer_names}
    niches = watchlist.get("niches") or {}
    for s in watchlist.get("streamers") or []:
        name = str(s["name"]).lower()
        if name in names_in_rules or name in text:
            return s["name"], int((niches.get(s.get("niche")) or {}).get("tier", 3))
    best = 3
    for niche in niches.values():
        if any(k.lower() in text for k in niche.get("keywords", [])):
            best = min(best, int(niche.get("tier", 3)))
    return None, best


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


class Scorer:
    def __init__(self, scoring: dict, watchlist: dict, country: str):
        self.cfg = scoring
        self.watchlist = watchlist
        self.country = country.upper()

    # ---- hard filters -------------------------------------------------
    def filters(self, c: Campaign, rules: Rules, streamer: str | None) -> list[str]:
        f = self.cfg.get("filters", {})
        fails = []
        if not c.status_open:
            fails.append("campaign is not open")
        pct = c.budget_left_pct
        if pct is not None and pct < f.get("min_budget_left_pct", 30):
            fails.append(f"only {pct:.0f}% of budget left")
        if c.spots_remaining is not None and c.spots_remaining <= 0:
            fails.append("no spots left")
        if c.allowed_countries and self.country not in c.allowed_countries:
            fails.append(f"{self.country} not allowed")
        mine = set(f.get("my_platforms", ["tiktok", "instagram", "youtube"]))
        if rules.platforms and not (set(rules.platforms) & mine):
            fails.append(f"platforms {rules.platforms} don't match ours")
        if streamer is None and not rules.source_footage:
            fails.append("no watchlist streamer and no supplied footage")
        if rules.requires_unsupported_format:
            fails.append("needs more than clipping (face-cam, voiceover or filming)")
        if rules.requires_promo_in_post:
            fails.append("requires a brand/casino name, logo, code or link in the post (platforms ban this)")
        return fails

    # ---- score parts, each 0..1 ---------------------------------------
    @staticmethod
    def pay_part(c: Campaign, history: list[Campaign]) -> float:
        if c.pay_per_1k_views is not None:
            mine, peers = c.pay_per_1k_views, [h.pay_per_1k_views for h in history if h.pay_per_1k_views]
        elif c.pay_per_submission is not None:
            mine, peers = c.pay_per_submission, [h.pay_per_submission for h in history if h.pay_per_submission]
        else:
            return 0.0
        if len(peers) < 3:
            return 0.5
        median = statistics.median(peers)
        return _clamp(mine / (2 * median)) if median > 0 else 0.5   # 2x the median = full marks

    @staticmethod
    def budget_part(c: Campaign) -> float:
        if c.budget_total is None:
            return 0.5
        pct = (c.budget_left_pct or 0) / 100
        size = _clamp((c.budget_left or 0) / 5000)   # $5,000+ left = full marks on size
        return 0.5 * pct + 0.5 * size

    def freshness_part(self, c: Campaign, now: datetime) -> float:
        if c.created_at is None:
            return 0.5
        cfg = self.cfg.get("freshness", {})
        boost, zero = cfg.get("boost_hours", 1), cfg.get("zero_after_hours", 48)
        created = c.created_at if c.created_at.tzinfo else c.created_at.replace(tzinfo=timezone.utc)
        age_h = (now - created).total_seconds() / 3600
        if age_h <= boost:
            return 1.0
        return _clamp(1 - (age_h - boost) / (zero - boost))

    @staticmethod
    def competition_part(c: Campaign) -> float:
        if c.unresolved_submissions is None or c.spots_remaining is None:
            return 0.5
        total = c.unresolved_submissions + c.spots_remaining
        return 1.0 if total == 0 else _clamp(1 - c.unresolved_submissions / total)

    def score(self, c: Campaign, rules: Rules, ready_clips: int, history: list[Campaign],
              now: datetime | None = None) -> ScoreResult:
        now = now or datetime.now(timezone.utc)
        streamer, tier = match_streamer(c, rules, self.watchlist)
        fails = self.filters(c, rules, streamer)
        if fails:
            return ScoreResult(campaign_id=c.id, passed=False, reasons=fails, streamer=streamer)
        w = self.cfg["weights"]
        tiers = {int(k): v for k, v in self.cfg.get("tiers", {1: 1.0, 2: 0.6, 3: 0.2}).items()}
        need = self.cfg.get("bank", {}).get("ready_clips_for_full_marks", 3)
        parts = {
            "pay_rate": self.pay_part(c, history),
            "budget_left": self.budget_part(c),
            "niche_fit": tiers.get(tier, 0.2),
            "bank_ready": _clamp(ready_clips / need),
            "freshness": self.freshness_part(c, now),
            "competition": self.competition_part(c),
        }
        breakdown = {k: round(parts[k] * w[k], 1) for k in w}
        total = round(sum(breakdown.values()), 1)
        reasons = [f"tier {tier} niche", f"{ready_clips} clips ready"]
        if rules.is_gambling:
            reasons.append("gambling: check 18+ / responsible-gambling line")
        if rules.unclear_fields:
            reasons.append("check this: " + ", ".join(rules.unclear_fields))
        return ScoreResult(campaign_id=c.id, passed=True, score=total, breakdown=breakdown,
                           reasons=reasons, streamer=streamer)

    def alert_kind(self, score: float, local_hour: int) -> str | None:
        a = self.cfg.get("alerts", {})
        if score >= a.get("instant_min_score", 75):
            start, end = a.get("quiet_hours", [23, 8])
            quiet = local_hour >= start or local_hour < end
            if quiet and score < a.get("quiet_override", 90):
                return "digest"
            return "instant"
        if score >= a.get("digest_min_score", 50):
            return "digest"
        return None
