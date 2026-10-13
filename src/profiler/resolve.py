"""Identifier resolution and publication gate module (Rules 1-6).

Responsibility: Accepts or rejects candidate facts using strict provenance & verification rules:
1. method api from a source keyed by orgnr -> accept.
2. website facts: accept only if the domain came from the BRREG website fact, or page text contains orgnr (formatted with or without spaces/dots) or exact legal name AND matching postal code.
3. third-party pages: accept only if orgnr appears in page text.
4. name-only evidence: always reject.
5. contradiction with registry (status, address, legal form): keep out of profile body, add to discrepancies.
6. consolidated accounts attached to a non-parent entity: reject.
"""

from dataclasses import dataclass
import re
from typing import Any, Dict, List, Optional
from profiler.models import Fact, clean_orgnr, normalize_orgnr, normalize_url


@dataclass
class Decision:
    """Decision object returned by the resolution publication gate."""

    accepted: bool
    reason: str
    downgraded: bool = False


def validate_norwegian_orgnr_checksum(orgnr: str) -> bool:
    """Validate Norwegian 9-digit organisation number using Modulo 11 checksum algorithm."""
    try:
        cleaned = clean_orgnr(orgnr)
    except ValueError:
        return False

    weights = [3, 2, 7, 6, 5, 4, 3, 2]
    total = sum(int(cleaned[i]) * weights[i] for i in range(8))
    remainder = total % 11

    if remainder == 0:
        control_digit = 0
    elif remainder == 1:
        return False  # Invalid control digit combination
    else:
        control_digit = 11 - remainder

    return int(cleaned[8]) == control_digit


def resolve_canonical_orgnr(raw_identifier: str) -> str:
    """Resolve and return canonical 9-digit organisation number string."""
    return clean_orgnr(raw_identifier)


def _extract_domain(url_or_domain: str) -> str:
    """Extract normalized registrable domain from URL or domain string (lowercase, drop scheme, www, path, query, and fragment)."""
    if not url_or_domain:
        return ""
    norm = normalize_url(str(url_or_domain))
    d = re.sub(r"^[a-z]+://", "", norm.lower())
    d = d.split("/")[0].split("?")[0].split("#")[0].split(":")[0]
    d = re.sub(r"^www\d*\.", "", d)
    return d.strip()


def check_domain(url: str, ctx: Any = None) -> bool:
    """Check if page URL passes the resolver domain check against the registry official website."""
    if not url:
        return False
    registry = getattr(ctx, "registry_facts", {}) if ctx else {}
    official_website = getattr(ctx, "official_website", None) or registry.get("website") or registry.get("hjemmeside")
    if not official_website:
        return False
    off_domain = _extract_domain(str(official_website))
    url_domain = _extract_domain(str(url))
    return bool(off_domain and url_domain and off_domain == url_domain)


def _contains_orgnr(text: str, target_orgnr: str) -> bool:
    """Check if target orgnr appears in text in any common format."""
    if not text or not target_orgnr:
        return False

    try:
        cleaned_target = clean_orgnr(target_orgnr)
    except ValueError:
        cleaned_target = str(target_orgnr)

    if not cleaned_target:
        return False

    # Direct digit match
    if cleaned_target in text:
        return True

    # Common formatted patterns e.g. "923 609 016", "923.609.016", "923-609-016"
    if len(cleaned_target) == 9:
        p1 = f"{cleaned_target[:3]} {cleaned_target[3:6]} {cleaned_target[6:]}"
        p2 = f"{cleaned_target[:3]}.{cleaned_target[3:6]}.{cleaned_target[6:]}"
        p3 = f"{cleaned_target[:3]}-{cleaned_target[3:6]}-{cleaned_target[6:]}"
        if p1 in text or p2 in text or p3 in text:
            return True

    # Check digit sequence in stripped text
    digits_only = re.sub(r"\D", "", text)
    if cleaned_target in digits_only:
        return True

    return False


def accept(fact: Fact, ctx: Any = None) -> Decision:
    """Publication gate evaluating a Fact against resolution rules in order."""
    registry: Dict[str, Any] = getattr(ctx, "registry_facts", {}) if ctx else {}
    target_orgnr: str = getattr(ctx, "orgnr", fact.orgnr) if ctx else fact.orgnr
    page_text: str = getattr(ctx, "page_text", None) or (fact.evidence_quote or "")
    official_website = getattr(ctx, "official_website", None) or registry.get("website") or registry.get("hjemmeside")
    postal_code = getattr(ctx, "postal_code", None) or registry.get("postal_code")
    legal_name = getattr(ctx, "legal_name", None) or registry.get("legal_name") or registry.get("name")
    is_parent: bool = (
        getattr(ctx, "is_parent", False)
        or bool(registry.get("sub_units"))
        or registry.get("is_group_parent") is True
    )

    # Rule 4: Name-only evidence ALWAYS rejected
    if any(mb.lower() == "name_only" for mb in fact.match_basis) or fact.match_basis == ["name_only"]:
        return Decision(
            accepted=False,
            reason="Rule 4: Name-only matching is strictly forbidden",
            downgraded=False,
        )

    # Wrong orgnr crossover check
    if fact.orgnr != target_orgnr:
        return Decision(
            accepted=False,
            reason=f"Crossover error: Fact orgnr '{fact.orgnr}' does not match target '{target_orgnr}'",
            downgraded=False,
        )

    # Rule 5: Registry contradiction
    if _contradicts_registry(fact, registry):
        return Decision(
            accepted=False,
            reason="Rule 5: Contradicts official registry data",
            downgraded=False,
        )

    # Rule 6: Consolidated accounts on non-parent entity
    if _is_unmatched_consolidated(fact, is_parent):
        return Decision(
            accepted=False,
            reason="Rule 6: Consolidated accounts attached to non-parent entity",
            downgraded=False,
        )

    # Rule 1: method api from an orgnr-keyed official source
    if fact.method == "api" and any(mb in ("orgnr_exact", "orgnr_exact_api_endpoint", "orgnr_query_param") for mb in fact.match_basis):
        return Decision(accepted=True, reason="Rule 1: Official API source keyed by orgnr", downgraded=False)

    # Rule 2: Website facts
    if fact.source_name.lower() in ("website", "web_scraper") or "http" in fact.source_url:
        domain_match = False
        if official_website:
            official_dom = _extract_domain(str(official_website))
            fact_dom = _extract_domain(fact.source_url)
            if official_dom and fact_dom and official_dom == fact_dom:
                domain_match = True

        orgnr_in_page = _contains_orgnr(page_text, target_orgnr)

        name_code_match = False
        if legal_name and postal_code and str(legal_name).lower() in page_text.lower() and str(postal_code) in page_text:
            name_code_match = True

        if domain_match or orgnr_in_page or name_code_match:
            return Decision(accepted=True, reason="Rule 2: Valid website fact with verified domain/orgnr proof", downgraded=False)
        else:
            return Decision(
                accepted=False,
                reason="Rule 2: Website fact missing official domain match, orgnr in page, or legal name + postal code",
                downgraded=False,
            )

    # Rule 3: Third-party pages
    if fact.method in ("parsed", "llm_verified") or fact.source_name.lower() not in (
        "enhetsregisteret",
        "regnskapsregisteret",
        "roller",
        "kunngjøringer",
    ):
        if page_text and not _contains_orgnr(page_text, target_orgnr):
            if not any(mb in ("orgnr_exact", "orgnr_exact_api_endpoint", "orgnr_query_param") for mb in fact.match_basis):
                return Decision(
                    accepted=False,
                    reason="Rule 3: Third-party page does not contain target orgnr in page text",
                    downgraded=False,
                )

    return Decision(accepted=True, reason="Accepted", downgraded=False)


def _contradicts_registry(fact: Fact, registry: Dict[str, Any]) -> bool:
    """Check if fact contradicts official registry facts."""
    if not registry:
        return False

    # Registry-sourced facts (method api, source data.brreg.no) must NEVER be compared against other registry facts
    if fact.method == "api" and (
        "data.brreg.no" in fact.source_url
        or fact.source_name.lower() in ("enhetsregisteret", "brreg", "regnskapsregisteret", "roller", "kunngjøringer")
    ):
        return False

    field = fact.field.lower()
    val = fact.value

    if field in ("status", "active") and "status" in registry:
        if str(val).lower() != str(registry["status"]).lower():
            return True

    if field in ("legal_form", "organization_type") and "legal_form" in registry:
        if str(val).lower() != str(registry["legal_form"]).lower():
            return True

    if field == "business_address" and "business_address" in registry:
        reg_addr = registry["business_address"]
        if isinstance(val, dict) and isinstance(reg_addr, dict):
            if val.get("postnummer") and reg_addr.get("postnummer"):
                if str(val["postnummer"]) != str(reg_addr["postnummer"]):
                    return True

    if field == "postal_address" and "postal_address" in registry:
        reg_addr = registry["postal_address"]
        if isinstance(val, dict) and isinstance(reg_addr, dict):
            if val.get("postnummer") and reg_addr.get("postnummer"):
                if str(val["postnummer"]) != str(reg_addr["postnummer"]):
                    return True

    return False


def _is_unmatched_consolidated(fact: Fact, is_parent: bool) -> bool:
    """Check if fact is consolidated financial accounts on a non-parent entity."""
    if fact.field == "financials" and isinstance(fact.value, dict):
        if fact.value.get("is_consolidated") is True and not is_parent:
            return True
    return False
