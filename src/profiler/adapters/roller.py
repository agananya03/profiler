"""Adapter for Brønnøysundregistrene Roller API (Rule 9).

Responsibility: Fetches official company roles (CEO, Chair, Board members, Auditor, Accountant)
from Brreg Roller API. Only publishes values provided by the source, stored as structured role objects
with resigned and deregistered flags.
"""

from typing import Any, Dict, List
import httpx

from profiler.adapters.base import BaseAdapter, Ctx
from profiler.models import Fact

ROLE_CATEGORY_MAP: Dict[str, str] = {
    "DAGL": "CEO",
    "LEDE": "Chair",
    "MEDL": "Board member",
    "NEST": "Deputy Chair",
    "OBSV": "Board observer",
    "REVI": "Auditor",
    "REGN": "Accountant",
    "PROK": "Attorney-in-fact",
}


class BrregRollerAdapter(BaseAdapter):
    """Adapter fetching official company roles from Brønnøysundregistrene Roller API."""

    @property
    def name(self) -> str:
        return "roller"

    async def fetch(self, orgnr: str, ctx: Ctx) -> List[Fact]:
        url = f"https://data.brreg.no/enhetsregisteret/api/enheter/{orgnr}/roller"
        match_basis = ["orgnr_exact"]
        method = "api"
        source_name = "Roller"
        confidence = 0.99
        facts: List[Fact] = []

        try:
            resp = await ctx.http.get(url)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()

            role_groups = data.get("rollegrupper", [])
            if not isinstance(role_groups, list):
                return []

            structured_roles: List[dict[str, Any]] = []

            for group in role_groups:
                if not isinstance(group, dict):
                    continue
                roles = group.get("roller", [])
                if not isinstance(roles, list):
                    continue

                for r in roles:
                    if not isinstance(r, dict):
                        continue

                    type_info = r.get("type", {})
                    code = type_info.get("kode", "")
                    desc = type_info.get("beskrivelse", "")
                    category = ROLE_CATEGORY_MAP.get(code, desc or code)

                    # Extract name & entity information
                    name = ""
                    entity_orgnr = None
                    if "person" in r and isinstance(r["person"], dict):
                        p_navn = r["person"].get("navn", {})
                        if isinstance(p_navn, dict):
                            parts = [
                                p_navn.get("fornavn"),
                                p_navn.get("mellomnavn"),
                                p_navn.get("etternavn"),
                            ]
                            name = " ".join([p for p in parts if p]).strip()
                    elif "enhet" in r and isinstance(r["enhet"], dict):
                        e_info = r["enhet"]
                        e_navn = e_info.get("navn")
                        if isinstance(e_navn, list):
                            name = " ".join(e_navn)
                        elif isinstance(e_navn, str):
                            name = e_navn
                        entity_orgnr = e_info.get("organisasjonsnummer")

                    resigned = bool(r.get("fratraadt", False))
                    deregistered = bool(r.get("avregistrert", False))

                    role_entry = {
                        "role_code": code,
                        "role_category": category,
                        "role_description": desc,
                        "name": name,
                        "entity_orgnr": entity_orgnr,
                        "resigned": resigned,
                        "deregistered": deregistered,
                    }
                    structured_roles.append(role_entry)

            if structured_roles:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="roles",
                        value=structured_roles,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )

                # Individual summary fields for active roles
                active_roles = [r for r in structured_roles if not r["resigned"] and not r["deregistered"]]

                ceo = next((r["name"] for r in active_roles if r["role_code"] == "DAGL"), None)
                if ceo:
                    facts.append(
                        Fact(
                            orgnr=orgnr,
                            field="ceo",
                            value=ceo,
                            source_name=source_name,
                            source_url=url,
                            retrieved_at=ctx.now,
                            method=method,
                            match_basis=match_basis,
                            confidence=confidence,
                        )
                    )

                chair = next((r["name"] for r in active_roles if r["role_code"] == "LEDE"), None)
                if chair:
                    facts.append(
                        Fact(
                            orgnr=orgnr,
                            field="chair",
                            value=chair,
                            source_name=source_name,
                            source_url=url,
                            retrieved_at=ctx.now,
                            method=method,
                            match_basis=match_basis,
                            confidence=confidence,
                        )
                    )

                auditor = next((r["name"] for r in active_roles if r["role_code"] == "REVI"), None)
                if auditor:
                    facts.append(
                        Fact(
                            orgnr=orgnr,
                            field="auditor",
                            value=auditor,
                            source_name=source_name,
                            source_url=url,
                            retrieved_at=ctx.now,
                            method=method,
                            match_basis=match_basis,
                            confidence=confidence,
                        )
                    )

                accountant = next((r["name"] for r in active_roles if r["role_code"] == "REGN"), None)
                if accountant:
                    facts.append(
                        Fact(
                            orgnr=orgnr,
                            field="accountant",
                            value=accountant,
                            source_name=source_name,
                            source_url=url,
                            retrieved_at=ctx.now,
                            method=method,
                            match_basis=match_basis,
                            confidence=confidence,
                        )
                    )

        except httpx.HTTPError as err:
            ctx.record_error(self.name, orgnr, "HTTP_ERROR", str(err))
        except Exception as err:
            ctx.record_error(self.name, orgnr, "UNEXPECTED_ERROR", str(err))

        return facts
