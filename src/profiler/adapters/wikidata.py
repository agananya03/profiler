"""Adapter for Wikidata SPARQL Query Service (Rule 9).

Responsibility: Queries Wikidata by Norwegian organisation number property (wdt:P2333).
Strictly rejects label/text search; relies purely on exact property matching.
Emits qid, description, inception, official_website, industry.
Skips if budget for 'enrich' priority is exhausted.
"""

from datetime import datetime, timezone
import logging
from typing import Any, List
import httpx

from profiler.adapters.base import BaseAdapter, Ctx
from profiler.models import Fact

logger = logging.getLogger(__name__)


class WikidataAdapter(BaseAdapter):
    """Adapter fetching structured metadata from Wikidata using property P2333 (Norwegian Org. Number)."""

    PROPERTY_ID = "P2333"

    @property
    def name(self) -> str:
        return "wikidata"

    async def fetch(self, orgnr: str, ctx: Ctx) -> List[Fact]:
        # Skip if budget for enrich priority is not affordable
        if not ctx.budget.can_afford(1, priority="enrich"):
            return []

        formatted_org = f"{orgnr[:3]} {orgnr[3:6]} {orgnr[6:]}" if len(orgnr) == 9 else orgnr
        registry_facts = getattr(ctx, "registry_facts", {})
        lei = registry_facts.get("lei")

        lei_union = f'UNION {{ ?item wdt:P1278 "{lei}" . }}' if lei else ""

        sparql_query = f"""
        SELECT ?item ?description ?inception ?official_website ?industryLabel WHERE {{
          {{ ?item wdt:{self.PROPERTY_ID} "{orgnr}" . }}
          UNION
          {{ ?item wdt:{self.PROPERTY_ID} "{formatted_org}" . }}
          {lei_union}
          OPTIONAL {{ ?item schema:description ?description . FILTER(LANG(?description) = "no" || LANG(?description) = "en") }}
          OPTIONAL {{ ?item wdt:P571 ?inception . }}
          OPTIONAL {{ ?item wdt:P856 ?official_website . }}
          OPTIONAL {{ ?item wdt:P452 ?industry . ?industry rdfs:label ?industryLabel . FILTER(LANG(?industryLabel) = "no" || LANG(?industryLabel) = "en") }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,no". }}
        }}
        """
        url = "https://query.wikidata.org/sparql"
        match_basis = ["wikidata_property_p2333"]
        method = "api"
        source_name = "Wikidata"
        now = ctx.now or datetime.now(timezone.utc)
        facts: List[Fact] = []

        headers = {
            "Accept": "application/sparql-results+json",
            "User-Agent": "NorwayProfiler/0.1.0 (https://github.com/norway-profiler; contact: admin@norway-profiler.no)",
        }

        try:
            resp = await ctx.http.get(
                url,
                params={"query": sparql_query, "format": "json"},
                headers=headers,
                priority="enrich",
            )
            if resp.status_code != 200:
                ctx.logger.info(f"[{self.name}] HTTP status: {resp.status_code}, result count: 0")
                if hasattr(ctx, "skipped_stages") and "wikidata" not in ctx.skipped_stages:
                    ctx.skipped_stages["wikidata"] = "no record for this orgnr"
                return []

            data: dict[str, Any] = resp.json()
            bindings = data.get("results", {}).get("bindings", [])
            result_count = len(bindings) if isinstance(bindings, list) else 0
            ctx.logger.info(f"[{self.name}] HTTP status: {resp.status_code}, result count: {result_count}")

            if not isinstance(bindings, list) or not bindings:
                if hasattr(ctx, "skipped_stages"):
                    ctx.skipped_stages["wikidata"] = "no record for this orgnr"
                return []

            item_data = bindings[0]

            # 1. QID
            item_uri = item_data.get("item", {}).get("value")
            if item_uri:
                qid = item_uri.split("/")[-1] if "/" in item_uri else item_uri
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="qid",
                        value=qid,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=now,
                        method=method,
                        match_basis=match_basis,
                        confidence=0.90,
                    )
                )

            # 2. Description
            desc = item_data.get("description", {}).get("value")
            if desc:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="description",
                        value=desc,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=now,
                        method=method,
                        match_basis=match_basis,
                        confidence=0.90,
                    )
                )

            # 3. Inception
            inception = item_data.get("inception", {}).get("value")
            if inception:
                inception_clean = inception.split("T")[0]
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="inception",
                        value=inception_clean,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=now,
                        method=method,
                        match_basis=match_basis,
                        confidence=0.90,
                    )
                )

            # 4. Official Website
            website = item_data.get("official_website", {}).get("value") or item_data.get("website", {}).get("value")
            if website:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="official_website",
                        value=website,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=now,
                        method=method,
                        match_basis=match_basis,
                        confidence=0.90,
                    )
                )

            # 5. Industry
            industry = item_data.get("industryLabel", {}).get("value") or item_data.get("industry", {}).get("value")
            if industry:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="industry",
                        value=industry,
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
