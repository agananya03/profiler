"""Fact conflict reconciliation and confidence score calculation module.

Responsibility:
- For each field, chooses the winning fact by source tier (registry > official register > structured data > llm_verified), then freshness.
- Records disagreements in profile.discrepancies as {field, chosen, rejected, reason}.
- Computes confidence scores using the documented formula in docs/CONFIDENCE.md.
"""

from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Sequence, Tuple

from profiler.models import Fact

# Tier 1: registry, Tier 2: official register, Tier 3: structured data, Tier 4: llm_verified
TIER_PRIORITIES: Dict[str, int] = {
    "registry": 4,
    "official register": 3,
    "structured data": 2,
    "llm_verified": 1,
}

TIER_WEIGHTS: Dict[str, float] = {
    "registry": 1.00,
    "official register": 0.90,
    "structured data": 0.80,
    "llm_verified": 0.70,
}


def get_source_tier(fact: Fact) -> str:
    """Determine source tier for a fact."""
    source_name = (fact.source_name or "").lower()
    method = (fact.method or "").lower()

    if method == "llm_verified" or source_name == "llm extraction":
        return "llm_verified"

    if any(s in source_name for s in ["enhetsregisteret", "regnskapsregisteret", "kunngjøringer", "roller", "brreg"]):
        return "registry"

    if any(s in source_name for s in ["gleif", "doffin"]):
        return "official register"

    if any(s in source_name for s in ["website", "wikidata"]):
        return "structured data"

    if method == "api":
        return "official register"

    return "structured data"


def get_match_basis_weight(match_basis: List[str]) -> float:
    """Calculate match basis weight."""
    if not match_basis:
        return 0.80
    mb_set = {mb.lower() for mb in match_basis}
    if "redirected_domain" in mb_set:
        return 0.70
    if any(mb in mb_set for mb in ["orgnr_exact", "orgnr_on_page", "wikidata_property_p2333", "supplier_orgnum"]):
        return 1.00
    if "domain_match" in mb_set:
        return 0.90
    return 0.85


def calculate_confidence(fact: Fact, agreeing_sources_count: int = 1, now: datetime = None) -> float:
    """Compute confidence score using documented formula in docs/CONFIDENCE.md."""
    now = now or datetime.now(timezone.utc)
    tier = get_source_tier(fact)
    w_tier = TIER_WEIGHTS.get(tier, 0.70)
    w_match = get_match_basis_weight(fact.match_basis)

    retrieved_at = fact.retrieved_at or now
    if retrieved_at.tzinfo is None:
        retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
    age_days = (now - retrieved_at).total_seconds() / 86400.0
    w_age = math.exp(-max(0.0, age_days) / 365.0)

    w_agree = 0.05 if agreeing_sources_count >= 2 else 0.00
    score = round(w_tier * w_match * w_age + w_agree, 4)
    return min(1.0, max(0.0, score))


def reconcile_facts_with_discrepancies(
    facts: Sequence[Fact],
    now: datetime = None,
) -> Tuple[List[Fact], List[Dict[str, Any]]]:
    """Reconcile facts per field choosing winning fact by source tier then freshness.

    Returns:
        Tuple of (reconciled_winning_facts, discrepancies_list).
    """
    now = now or datetime.now(timezone.utc)
    grouped: Dict[str, List[Fact]] = {}
    for f in facts:
        grouped.setdefault(f.field, []).append(f)

    reconciled: List[Fact] = []
    discrepancies: List[Dict[str, Any]] = []

    for field_name, fact_group in grouped.items():
        # Sort key: (tier_priority, timestamp) descending
        sorted_facts = sorted(
            fact_group,
            key=lambda f: (
                TIER_PRIORITIES.get(get_source_tier(f), 0),
                f.retrieved_at or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )

        winning_fact = sorted_facts[0]

        # Count how many independent sources agree on the exact winning value
        winning_val_str = str(winning_fact.value)
        agreeing_sources = {
            f.source_name for f in fact_group if str(f.value) == winning_val_str
        }
        agreeing_count = len(agreeing_sources)

        # Update winning fact confidence using formula
        winning_fact.confidence = calculate_confidence(winning_fact, agreeing_sources_count=agreeing_count, now=now)
        reconciled.append(winning_fact)

        # Check for disagreements among non-winning facts
        for rejected_fact in sorted_facts[1:]:
            if str(rejected_fact.value) != winning_val_str:
                win_tier = get_source_tier(winning_fact)
                rej_tier = get_source_tier(rejected_fact)
                discrepancy_entry = {
                    "field": field_name,
                    "chosen": winning_fact.value,
                    "rejected": rejected_fact.value,
                    "reason": (
                        f"Chosen higher priority source '{winning_fact.source_name}' ({win_tier}) "
                        f"over '{rejected_fact.source_name}' ({rej_tier})"
                    ),
                }
                discrepancies.append(discrepancy_entry)

    return reconciled, discrepancies


def reconcile_facts(facts: Sequence[Fact]) -> List[Fact]:
    """Backward compatibility helper for single-list fact reconciliation."""
    reconciled, _ = reconcile_facts_with_discrepancies(facts)
    return reconciled
