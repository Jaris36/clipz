"""Turns a campaign brief into a Rules checklist with a small Claude model.

Results are cached in campaign_rules by a hash of the description, so an unchanged
brief is never read twice. With no API key, a keyword fallback still catches the
gambling-promo and platform rules so filters keep working offline.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from clipz.scout.models import Campaign, Rules

MODEL = "claude-haiku-5-5"

SYSTEM = """You read paid clipping campaign briefs and extract the posting rules.
Only report what the brief says. If a field is not mentioned, leave it empty.
If the brief is ambiguous about a field, add that field's name to unclear_fields.
Platforms must be lowercase from: tiktok, instagram, youtube, x, other.
Set is_gambling when the streamer, content or sponsor involves gambling or casinos.
Set requires_promo_in_post when the post itself must show a casino or brand name,
logo, promo code, referral code or sign-up link.
Set requires_unsupported_format when the clip needs a face-cam reaction, original
voiceover, original filming or anything beyond cutting and captioning existing footage."""


def description_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


_PLATFORM_WORDS = {
    "tiktok": ["tiktok", "tik tok"],
    "instagram": ["instagram", "reels", " ig "],
    "youtube": ["youtube", "shorts", "yt shorts"],
    "x": ["twitter", " x.com"],
}
_GAMBLING_WORDS = ["casino", "gambling", "slots", "stake.com", "roobet", "betting", "sportsbook"]
_PROMO_PATTERNS = [r"promo code", r"referral code", r"use code", r"affiliate link", r"sign[- ]?up link",
                   r"show (the )?logo", r"include (the )?(casino|brand) name"]


def keyword_rules(text: str) -> Rules:
    """Cheap offline fallback. Less accurate than the model; marks itself unclear."""
    low = f" {text.lower()} "
    platforms = [p for p, words in _PLATFORM_WORDS.items() if any(w in low for w in words)]
    hashtags = sorted(set(re.findall(r"#\w+", text)))
    tags = sorted(set(re.findall(r"(?<!\w)@\w+", text)))
    return Rules(
        platforms=platforms,
        required_hashtags=hashtags,
        required_tags=tags,
        is_gambling=any(w in low for w in _GAMBLING_WORDS),
        requires_promo_in_post=any(re.search(p, low) for p in _PROMO_PATTERNS),
        unclear_fields=["all (read by keywords, not AI)"],
    )


class RulesReader:
    def __init__(self, conn: sqlite3.Connection, api_key: str | None = None, client=None):
        self._conn = conn
        self._client = client
        if self._client is None and api_key:
            import anthropic

            self._client = anthropic.Anthropic(api_key=api_key)

    def _cached(self, campaign_id: str, dhash: str) -> Rules | None:
        row = self._conn.execute(
            "SELECT rules_json, description_hash FROM campaign_rules WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()
        if row and row["description_hash"] == dhash:
            return Rules.model_validate_json(row["rules_json"])
        return None

    def _ask_model(self, campaign: Campaign) -> Rules | None:
        if self._client is None:
            return None
        brief = f"Title: {campaign.title}\n\nBrief:\n{campaign.description}"
        try:
            response = self._client.messages.parse(
                model=MODEL,
                max_tokens=2000,
                system=SYSTEM,
                messages=[{"role": "user", "content": brief}],
                output_format=Rules,
                output_config={"effort": "low"},
            )
        except Exception as exc:  # network or API error: fall back, retry next poll
            print(f"[rules_reader] model call failed for {campaign.id}: {type(exc).__name__}: {exc}")
            return None
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return None
        return response.parsed_output

    def read(self, campaign: Campaign) -> Rules:
        dhash = description_hash(campaign.title + "\n" + campaign.description)
        cached = self._cached(campaign.id, dhash)
        if cached is not None:
            return cached
        rules = self._ask_model(campaign)
        from_model = rules is not None
        if rules is None:
            rules = keyword_rules(campaign.title + "\n" + campaign.description)
        if from_model:  # only cache model output; keyword results get retried with the model later
            self._conn.execute(
                "INSERT INTO campaign_rules (campaign_id, description_hash, rules_json) VALUES (?, ?, ?) "
                "ON CONFLICT(campaign_id) DO UPDATE SET description_hash = excluded.description_hash, "
                "rules_json = excluded.rules_json, read_at = datetime('now')",
                (campaign.id, dhash, rules.model_dump_json()),
            )
            self._conn.commit()
        return rules
