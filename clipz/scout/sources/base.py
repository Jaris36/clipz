"""Every campaign source returns a list of normalized Campaigns."""
from __future__ import annotations

from typing import Protocol

from clipz.scout.models import Campaign


class CampaignSource(Protocol):
    name: str

    def fetch(self) -> list[Campaign]:
        """Return the currently open campaigns this source can see."""
        ...
