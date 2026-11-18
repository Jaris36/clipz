"""Data shapes the scout passes around."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class CampaignStatus(str, Enum):
    NEW = "new"            # seen, not yet alerted
    ALERTED = "alerted"    # Preston was pinged
    JOINED = "joined"      # Preston approved and joined
    SKIPPED = "skipped"    # failed a filter or Preston tapped Skip
    DRAINED = "drained"    # budget ran out or campaign closed


class Campaign(BaseModel):
    """One clipping campaign, normalized from any source."""

    id: str                                  # our id: "<source>:<source_id>"
    source: str                              # whop_bounty | manual | apify
    source_id: str
    title: str
    description: str = ""
    link: str | None = None
    status_open: bool = True
    # Pay: Content Rewards pays per 1,000 views; Whop bounties pay per accepted submission.
    pay_per_1k_views: float | None = None
    pay_per_submission: float | None = None
    currency: str = "usd"
    budget_total: float | None = None
    budget_paid_out: float | None = None
    spots_remaining: int | None = None
    unresolved_submissions: int | None = None
    allowed_countries: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    deadline: datetime | None = None

    @property
    def budget_left(self) -> float | None:
        if self.budget_total is None:
            return None
        return max(self.budget_total - (self.budget_paid_out or 0.0), 0.0)

    @property
    def budget_left_pct(self) -> float | None:
        if not self.budget_total:
            return None
        return 100.0 * (self.budget_left or 0.0) / self.budget_total


class Rules(BaseModel):
    """Checklist the rule reader pulls out of a campaign brief."""

    platforms: list[str] = Field(default_factory=list, description="Lowercase: tiktok, instagram, youtube, x, other")
    streamer_names: list[str] = Field(default_factory=list, description="Streamers or creators whose content is clipped")
    required_tags: list[str] = Field(default_factory=list, description="@mentions that must be in the caption")
    required_hashtags: list[str] = Field(default_factory=list)
    disclosure_text: str | None = Field(None, description="Exact disclosure wording required, if any")
    min_length_seconds: int | None = None
    max_length_seconds: int | None = None
    source_footage: str | None = Field(None, description="Where clips must come from: dates, a footage link, etc.")
    banned_content: list[str] = Field(default_factory=list)
    max_clips_per_person: int | None = None
    min_views_for_payout: int | None = None
    language: str | None = None
    regions: list[str] = Field(default_factory=list)
    is_gambling: bool = Field(False, description="True if the content or sponsor is gambling or a casino")
    requires_promo_in_post: bool = Field(
        False, description="True if the post must show a casino/brand name, logo, promo code or sign-up link"
    )
    requires_unsupported_format: bool = Field(
        False, description="True if it needs face-cam reaction, original voiceover or anything beyond clipping"
    )
    unclear_fields: list[str] = Field(default_factory=list, description="Fields the brief was ambiguous about")


class Clip(BaseModel):
    """A banked clip as the scout sees it (the pipeline owns the clips table)."""

    id: str
    streamer: str
    length_seconds: float
    moment_score: float
    hook_title: str = ""
    file_path: str = ""
    cut_at: datetime


class ScoreResult(BaseModel):
    campaign_id: str
    passed: bool
    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)   # why it failed, or what drove the score
    breakdown: dict[str, float] = Field(default_factory=dict)
    streamer: str | None = None
