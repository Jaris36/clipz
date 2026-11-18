"""Campaigns Preston adds by hand (streamer Discord programs, anything the API misses).

Phase 1 reads config/manual_campaigns.yaml. Phase 2 adds a /add command in Telegram
that writes to the same file.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from clipz.scout.models import Campaign


class ManualSource:
    name = "manual"

    def __init__(self, path: Path):
        self._path = path

    def fetch(self) -> list[Campaign]:
        if not self._path.exists():
            return []
        rows = yaml.safe_load(self._path.read_text(encoding="utf-8")) or []
        out = []
        for row in rows:
            sid = str(row["id"])
            out.append(Campaign(id=f"manual:{sid}", source="manual", source_id=sid,
                                **{k: v for k, v in row.items() if k != "id"}))
        return out
