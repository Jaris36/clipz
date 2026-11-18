"""Loads settings from environment variables and the YAML files in config/."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass
class Settings:
    db_path: str
    country: str
    whop_token: str | None
    anthropic_api_key: str | None
    scoring: dict = field(default_factory=dict)
    watchlist: dict = field(default_factory=dict)

    @classmethod
    def load(cls, config_dir: Path | None = None) -> "Settings":
        config_dir = config_dir or ROOT / "config"
        return cls(
            db_path=os.environ.get("CLIPZ_DB_PATH", str(ROOT / "data" / "clipz.db")),
            country=os.environ.get("CLIPZ_COUNTRY", "US"),
            whop_token=os.environ.get("WHOP_TOKEN") or None,
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            scoring=_load_yaml(config_dir / "scoring.yaml"),
            watchlist=_load_yaml(config_dir / "watchlist.yaml"),
        )
