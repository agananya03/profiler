"""Adapter for Brønnøysundregistrene Enhetsregisteret API (Rule 9).

Responsibility: Fetches master company details (legal name, form, addresses, NACE codes,
employee count, founding/registration dates, website, VAT/business register flags,
status flags, parent orgnr, share capital) from official Brreg Enhetsregisteret API.
Supports fallback to /underenheter for branches and handles deleted entities.
"""

from typing import Any, List, Optional
import httpx

from profiler.adapters.base import BaseAdapter, Ctx
from profiler.models import Fact, normalize_url


class BrregEnhetAdapter(BaseAdapter):
    """Adapter fetching company master data from Brønnøysundregistrene Enhetsregisteret API."""

    @property
    def name(self) -> str:
        return "brreg"

    async def fetch(self, orgnr: str, ctx: Ctx) -> List[Fact]:
        match_basis = ["orgnr_exact"]
        method = "api"
        source_name = "Enhetsregisteret"
        confidence = 0.99
        facts: List[Fact] = []

        url = f"https://data.brreg.no/enhetsregisteret/api/enheter/{orgnr}"
        is_branch = False

        try:
            resp = await ctx.http.get(url)
            if resp.status_code == 404:
                # Fall back to underenheter endpoint
                under_url = f"https://data.brreg.no/enhetsregisteret/api/underenheter/{orgnr}"
                resp_under = await ctx.http.get(under_url)
                if resp_under.status_code == 404:
                    ctx.record_error(
                        self.name, orgnr, "NOT_FOUND", f"Org {orgnr} not found in Enhetsregisteret."
                    )
                    return []
                resp = resp_under
                url = under_url
                is_branch = True

            resp.raise_for_status()
            data: dict[str, Any] = resp.json()

            body_orgnr = str(data.get("organisasjonsnummer", ""))
            if body_orgnr and body_orgnr != orgnr:
                ctx.record_error(
                    self.name,
                    orgnr,
                    "MATCH_ERROR",
                    f"API returned orgnr '{body_orgnr}' which does not match requested '{orgnr}'",
                )
                return []

            # Check if entity is deleted/removed
            is_deleted = bool(data.get("slettet") or "slettedato" in data)
            if is_deleted:
                slettedato = data.get("slettedato")
                slettingstype = data.get("slettingstype")
                reason = slettingstype or (f"Deleted on {slettedato}" if slettedato else "Entity deleted/removed")
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="status",
                        value="inactive",
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="status_reason",
                        value=reason,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )

            # 1. legal_name
            if "navn" in data:
                navn = data["navn"]
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="legal_name",
                        value=navn,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                        source_date=data.get("registreringsdatoEnhetsregisteret"),
                    )
                )

            # 2. legal_form / organization_type
            if is_branch:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="legal_form",
                        value="branch",
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )
            else:
                org_form = data.get("organisasjonsform", {})
                if isinstance(org_form, dict) and "kode" in org_form:
                    form_code = org_form.get("kode")
                    facts.append(
                        Fact(
                            orgnr=orgnr,
                            field="legal_form",
                            value=form_code,
                            source_name=source_name,
                            source_url=url,
                            retrieved_at=ctx.now,
                            method=method,
                            match_basis=match_basis,
                            confidence=confidence,
                        )
                    )

            # 3. postal_address
            if "postadresse" in data:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="postal_address",
                        value=data["postadresse"],
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )

            # 4. business_address
            bus_addr = data.get("forretningsadresse") or data.get("beliggenhetsadresse")
            if bus_addr:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="business_address",
                        value=bus_addr,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )

            # 5. nace_codes (with readable labels)
            nace_list: List[dict[str, str]] = []
            for nace_key in ("naeringskode1", "naeringskode2", "naeringskode3"):
                nace_obj = data.get(nace_key)
                if isinstance(nace_obj, dict) and "kode" in nace_obj:
                    nace_info = {
                        "code": nace_obj["kode"],
                        "description": nace_obj.get("beskrivelse", ""),
                    }
                    nace_list.append(nace_info)

            if nace_list:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="nace_codes",
                        value=nace_list,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )

            # 6. employee_count
            if "antallAnsatte" in data:
                emp_cnt = data["antallAnsatte"]
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="employee_count",
                        value=emp_cnt,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )

            # 7. founding_date
            if "stiftelsesdato" in data:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="founding_date",
                        value=data["stiftelsesdato"],
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                        source_date=data["stiftelsesdato"],
                    )
                )

            # 8. registration_date
            if "registreringsdatoEnhetsregisteret" in data:
                reg_date = data["registreringsdatoEnhetsregisteret"]
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="registration_date",
                        value=reg_date,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                        source_date=reg_date,
                    )
                )

            # 9. website
            if "hjemmeside" in data and data["hjemmeside"]:
                norm_website = normalize_url(data["hjemmeside"])
                if norm_website:
                    facts.append(
                        Fact(
                            orgnr=orgnr,
                            field="website",
                            value=norm_website,
                            source_name=source_name,
                            source_url=url,
                            retrieved_at=ctx.now,
                            method=method,
                            match_basis=match_basis,
                            confidence=confidence,
                        )
                    )

            # 10. vat_registered
            if "registrertIMvaregisteret" in data:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="vat_registered",
                        value=bool(data["registrertIMvaregisteret"]),
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )

            # 11. in_business_register
            if "registrertIForetaksregisteret" in data:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="in_business_register",
                        value=bool(data["registrertIForetaksregisteret"]),
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )

            # 12. is_bankrupt
            if "konkurs" in data:
                is_bankrupt = bool(data["konkurs"])
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="is_bankrupt",
                        value=is_bankrupt,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )

            # 13. under_liquidation
            if "underAvvikling" in data:
                is_liq = bool(data["underAvvikling"])
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="under_liquidation",
                        value=is_liq,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )

            # 14. under_forced_liquidation
            if "underTvangsavviklingEllerTvangsopplosning" in data:
                is_forced_liq = bool(data["underTvangsavviklingEllerTvangsopplosning"])
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="under_forced_liquidation",
                        value=is_forced_liq,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )

            # 15. parent_orgnr
            parent_info = data.get("overordnetEnhet")
            if parent_info:
                p_orgnr = None
                if isinstance(parent_info, str):
                    p_orgnr = parent_info
                elif isinstance(parent_info, dict):
                    if "organisasjonsnummer" in parent_info:
                        p_orgnr = str(parent_info["organisasjonsnummer"])
                    elif "href" in parent_info:
                        p_orgnr = parent_info["href"].rstrip("/").split("/")[-1]
                if p_orgnr:
                    facts.append(
                        Fact(
                            orgnr=orgnr,
                            field="parent_orgnr",
                            value=p_orgnr,
                            source_name=source_name,
                            source_url=url,
                            retrieved_at=ctx.now,
                            method=method,
                            match_basis=match_basis,
                            confidence=confidence,
                        )
                    )

            # 16. share_capital if present
            share_cap = data.get("aksjekapital") or data.get("kapital")
            if share_cap is not None:
                if isinstance(share_cap, dict) and "belop" in share_cap:
                    share_cap_val = share_cap["belop"]
                else:
                    share_cap_val = share_cap
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="share_capital",
                        value=share_cap_val,
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )

            # 17. Fetch sub-units/branches for parent organization (capped at 25)
            if not is_branch:
                try:
                    sub_units_url = f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={orgnr}"
                    sub_resp = await ctx.http.get(sub_units_url)
                    if sub_resp.status_code == 200:
                        sub_data = sub_resp.json()
                        raw_branches = sub_data.get("_embedded", {}).get("underenheter", []) or sub_data.get("underenheter", [])
                        page_info = sub_data.get("page", {})
                        total_count = page_info.get("totalElements", len(raw_branches))

                        branches_list = []
                        for b in raw_branches:
                            if isinstance(b, dict):
                                b_org = b.get("organisasjonsnummer")
                                b_name = b.get("navn")
                                b_addr = b.get("beliggenhetsadresse") or b.get("forretningsadresse")
                                b_nace = b.get("naeringskode1", {}).get("kode") if isinstance(b.get("naeringskode1"), dict) else None
                                branches_list.append({
                                    "orgnr": b_org,
                                    "name": b_name,
                                    "address": b_addr,
                                    "nace_code": b_nace,
                                })

                        is_truncated = total_count > 25 or len(raw_branches) > 25
                        if branches_list:
                            facts.append(
                                Fact(
                                    orgnr=orgnr,
                                    field="sub_units",
                                    value={
                                        "total_count": total_count,
                                        "truncated": is_truncated,
                                        "branches": branches_list[:25],
                                    },
                                    source_name=source_name,
                                    source_url=sub_units_url,
                                    retrieved_at=ctx.now,
                                    method=method,
                                    match_basis=match_basis,
                                    confidence=confidence,
                                )
                            )
                except Exception:
                    pass

        except httpx.HTTPError as err:
            ctx.record_error(self.name, orgnr, "HTTP_ERROR", str(err))
        except Exception as err:
            ctx.record_error(self.name, orgnr, "UNEXPECTED_ERROR", str(err))

        return facts
