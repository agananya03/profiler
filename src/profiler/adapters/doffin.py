"""Adapter for Doffin Norwegian public procurement database (Rule 9).

Responsibility: Fetches public procurement notices where the orgnr appears.
Emits up to the 5 most recent contracts (title, buyer, date, URL).
Skips if budget for 'enrich' priority is exhausted.
"""

from datetime import datetime, timezone
import os
from typing import Any, List
import httpx

from profiler.adapters.base import BaseAdapter, Ctx
from profiler.models import Fact


class DoffinAdapter(BaseAdapter):
    """Adapter fetching public procurement contract records from Doffin."""

    @property
    def name(self) -> str:
        return "doffin"

    async def fetch(self, orgnr: str, ctx: Ctx) -> List[Fact]:
        api_key = os.getenv("DOFFIN_API_KEY")
        if not api_key:
            # Skip when key is missing: make no request
            return []

        # Skip if budget for enrich priority is not affordable
        if not ctx.budget.can_afford(1, priority="enrich"):
            return []

        url = "https://api.doffin.no/public/v2/notices"
        headers = {"Ocp-Apim-Subscription-Key": api_key}
        params = {"supplierOrgNum": orgnr}
        match_basis = ["supplier_orgnum"]
        method = "api"
        source_name = "Doffin"
        now = ctx.now or datetime.now(timezone.utc)
        facts: List[Fact] = []

        try:
            resp = await ctx.http.get(url, params=params, headers=headers, priority="enrich")
            if resp.status_code != 200:
                return []

            data: dict[str, Any] = resp.json()
            raw_results = data.get("results") or data.get("notices") or []
            if not isinstance(raw_results, list) or not raw_results:
                return []

            contracts: List[dict] = []
            for item in raw_results:
                if not isinstance(item, dict):
                    continue

                title = item.get("title") or item.get("noticeTitle") or "Public Contract"
                buyer = (
                    item.get("buyer")
                    or item.get("buyerName")
                    or item.get("contractingAuthority")
                    or "Unknown Buyer"
                )
                date_str = (
                    item.get("date")
                    or item.get("publicationDate")
                    or item.get("issueDate")
                    or ""
                )
                notice_url = (
                    item.get("url")
                    or item.get("noticeUrl")
                    or f"https://doffin.no/notices/{item.get('id', '')}"
                )

                contracts.append(
                    {
                        "title": str(title),
                        "buyer": str(buyer),
                        "date": str(date_str),
                        "url": str(notice_url),
                    }
                )

            if not contracts:
                return []

            # Sort by date descending and cap at 5 most recent contracts
            contracts.sort(key=lambda c: c["date"], reverse=True)
            top_5_contracts = contracts[:5]

            facts.append(
                Fact(
                    orgnr=orgnr,
                    field="public_contracts",
                    value=top_5_contracts,
                    source_name=source_name,
                    source_url=url,
                    retrieved_at=now,
                    method=method,
                    match_basis=match_basis,
                    confidence=0.90,
                )
            )

        except httpx.HTTPError as err:
            ctx.record_error(self.name, orgnr, "HTTP_ERROR", str(err))
        except Exception as err:
            ctx.record_error(self.name, orgnr, "UNEXPECTED_ERROR", str(err))

        return facts
