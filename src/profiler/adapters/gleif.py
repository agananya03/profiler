"""Adapter for GLEIF API (Legal Entity Identifier) (Rule 9).

Responsibility: Looks up LEI records by Norwegian org number.
Emits lei, legal_name, status, direct_parent, and ultimate_parent LEIs.
Skips if budget for 'enrich' priority is exhausted.
"""

from datetime import datetime, timezone
from typing import Any, List
import httpx

from profiler.adapters.base import BaseAdapter, Ctx
from profiler.models import Fact


class GleifAdapter(BaseAdapter):
    """Adapter fetching LEI records from the Global Legal Entity Identifier Foundation API."""

    @property
    def name(self) -> str:
        return "gleif"

    async def fetch(self, orgnr: str, ctx: Ctx) -> List[Fact]:
        # Skip if budget for enrich priority is not affordable
        if not ctx.budget.can_afford(1, priority="enrich"):
            return []

        import urllib.parse
        formatted_org = f"{orgnr[:3]} {orgnr[3:6]} {orgnr[6:]}" if len(orgnr) == 9 else orgnr
        url = f"https://api.gleif.org/api/v1/lei-records?filter[entity.registeredAs]={urllib.parse.quote(formatted_org)}"
        match_basis = ["orgnr_exact"]
        method = "api"
        source_name = "GLEIF"
        now = ctx.now or datetime.now(timezone.utc)
        facts: List[Fact] = []

        try:
            resp = await ctx.http.get(url, priority="enrich")
            data: dict[str, Any] = resp.json() if resp.status_code == 200 else {}
            records = data.get("data", [])

            if not isinstance(records, list) or not records:
                return []

            # Accept a record only if registeredAt.id == "RA000472" and legalAddress.country == "NO"
            matching_rec = None
            for rec in records:
                entity = rec.get("attributes", {}).get("entity", {})
                reg_id = entity.get("registeredAt", {}).get("id")
                country = entity.get("legalAddress", {}).get("country")
                if reg_id == "RA000472" and country == "NO":
                    matching_rec = rec
                    break

            if not matching_rec:
                return []

            first_rec = matching_rec
            attrs = first_rec.get("attributes", {})
            rel = first_rec.get("relationships", {})

            # 1. LEI ID
            lei_id = attrs.get("lei") or first_rec.get("id")
            if lei_id:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="lei",
                        value=lei_id,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=now,
                        method=method,
                        match_basis=match_basis,
                        confidence=0.99,
                    )
                )

            # 2. Legal Name
            entity = attrs.get("entity", {})
            legal_name_obj = entity.get("legalName", {})
            legal_name = (
                legal_name_obj.get("name")
                if isinstance(legal_name_obj, dict)
                else legal_name_obj
            )
            if legal_name:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="legal_name",
                        value=legal_name,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=now,
                        method=method,
                        match_basis=match_basis,
                        confidence=0.99,
                    )
                )

            # 3. Status
            status = entity.get("status") or attrs.get("registration", {}).get("status")
            if status:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="status",
                        value=status,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=now,
                        method=method,
                        match_basis=match_basis,
                        confidence=0.99,
                    )
                )

            # 4. Direct Parent LEI
            dp_data = rel.get("direct-parent", {}).get("data", {})
            if isinstance(dp_data, dict):
                dp_lei = dp_data.get("id") or dp_data.get("attributes", {}).get("lei")
                if dp_lei:
                    facts.append(
                        Fact(
                            orgnr=orgnr,
                            field="direct_parent",
                            value=dp_lei,
                            source_name=source_name,
                            source_url=url,
                            retrieved_at=now,
                            method=method,
                            match_basis=match_basis,
                            confidence=0.99,
                        )
                    )

            # 5. Ultimate Parent LEI
            up_data = rel.get("ultimate-parent", {}).get("data", {})
            if isinstance(up_data, dict):
                up_lei = up_data.get("id") or up_data.get("attributes", {}).get("lei")
                if up_lei:
                    facts.append(
                        Fact(
                            orgnr=orgnr,
                            field="ultimate_parent",
                            value=up_lei,
                            source_name=source_name,
                            source_url=url,
                            retrieved_at=now,
                            method=method,
                            match_basis=match_basis,
                            confidence=0.99,
                        )
                    )

        except httpx.HTTPError as err:
            ctx.record_error(self.name, orgnr, "HTTP_ERROR", str(err))
        except Exception as err:
            ctx.record_error(self.name, orgnr, "UNEXPECTED_ERROR", str(err))

        return facts
