"""Adapter for Brønnøysundregistrene Kunngjøringer API (Rule 9).

Responsibility: Fetches official statutory announcements (bankruptcy, liquidation, merger,
demerger, name change, capital change) for an organisation number with announcement date and URL.
"""

from typing import Any, Dict, List
import httpx

from profiler.adapters.base import BaseAdapter, Ctx
from profiler.models import Fact


def _classify_announcement(title: str) -> str:
    t = title.lower()
    if any(w in t for w in ["konkurs", "tvangsavvikling", "tvangsoppløsning"]):
        return "bankruptcy"
    if any(w in t for w in ["oppløsning", "avvikling", "sletting"]):
        return "liquidation"
    if "fusjon" in t:
        return "merger"
    if "fisjon" in t:
        return "demerger"
    if any(w in t for w in ["navn", "navneendring"]):
        return "name_change"
    if any(w in t for w in ["kapital", "aksjekapital"]):
        return "capital_change"
    return "other"


class BrregKunngjoringAdapter(BaseAdapter):
    """Adapter for Brønnøysundregistrene Kunngjøringer.

    NOTE: Not used in profiling runs as there is no documented public JSON API
    for Kunngjøringer. Lifecycle status is taken from Enhetsregisteret fields.
    """

    @property
    def name(self) -> str:
        return "kunngjoring"

    async def fetch(self, orgnr: str, ctx: Ctx) -> List[Fact]:
        # No documented public JSON API exists for Kunngjøringer.
        # Calls to all Kunngjøringer endpoints and fallbacks are removed.
        # Lifecycle status is taken directly from Enhetsregisteret fields.
        return []

