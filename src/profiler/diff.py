"""Profile diffing and change detection module.

Responsibility:
- Compares new facts with stored ones by content_hash and normalized value.
- Produces Change objects with materiality ranking:
  bankruptcy > liquidation > merger > CEO change > revenue drop over 30% > address/name change > other.
- Handles name change (keep history), address move, new CEO, status to inactive, revenue change, merger, and dissolution (mark inactive, never delete).
"""

from datetime import datetime, timezone
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from profiler.models import Change, Fact, Profile


def normalize_value_str(val: Any) -> str:
    """Normalize value representation for comparison."""
    if val is None:
        return ""
    if isinstance(val, (dict, list)):
        return json.dumps(val, sort_keys=True)
    return re.sub(r"\s+", " ", str(val)).strip().lower()


def determine_materiality(field: str, old_val: Any, new_val: Any) -> str:
    """Rank change materiality based on domain business rules:
    bankruptcy > liquidation > merger > CEO change > revenue drop over 30% > address/name change > other.
    """
    f_lower = field.lower()

    # 1. Bankruptcy
    if "bankrupt" in f_lower:
        return "high"

    # 2. Liquidation
    if "liquidat" in f_lower:
        return "high"

    # 3. Merger
    if "merger" in f_lower or "demerger" in f_lower:
        return "high"

    # 4. CEO change
    if f_lower == "ceo" or (f_lower == "roles" and "ceo" in str(old_val).lower()):
        return "high"

    # 5. Status to inactive / dissolution
    if f_lower == "status" and str(new_val).lower() == "inactive":
        return "high"

    # 6. Revenue drop over 30%
    if f_lower in ("revenue", "driftsinntekter", "financials"):
        try:
            old_rev = old_val if isinstance(old_val, (int, float)) else (old_val.get("revenue") if isinstance(old_val, dict) else float(old_val))
            new_rev = new_val if isinstance(new_val, (int, float)) else (new_val.get("revenue") if isinstance(new_val, dict) else float(new_val))
            if old_rev and old_rev > 0 and new_rev is not None:
                drop_ratio = (old_rev - new_rev) / old_rev
                if drop_ratio >= 0.30:
                    return "medium"
        except Exception:
            pass

    # 7. Address / Name change
    if f_lower in ("name", "legal_name", "address", "registered_address", "business_address", "postal_address"):
        return "medium"

    return "low"


class ProfileDiff:
    """Represents differences between two profiles or fact sets."""

    def __init__(self, added_facts: List[Fact], removed_facts: List[Fact], changes: List[Change]) -> None:
        self.added_facts = added_facts
        self.removed_facts = removed_facts
        self.changes = changes

    def has_changes(self) -> bool:
        return bool(self.added_facts or self.removed_facts or self.changes)


def diff_facts(old_facts: Sequence[Fact], new_facts: Sequence[Fact]) -> ProfileDiff:
    """Compare old and new facts by content_hash and normalized value to compute delta changes."""
    old_hash_map = {f.content_hash: f for f in old_facts if f.content_hash}
    new_hash_map = {f.content_hash: f for f in new_facts if f.content_hash}

    added = [f for ch, f in new_hash_map.items() if ch not in old_hash_map]
    removed = [f for ch, f in old_hash_map.items() if ch not in new_hash_map]

    changes: List[Change] = []
    old_fields = {f.field: f for f in old_facts}
    new_fields = {f.field: f for f in new_facts}

    for field, new_fact in new_fields.items():
        if field in old_fields:
            old_fact = old_fields[field]
            old_norm = normalize_value_str(old_fact.value)
            new_norm = normalize_value_str(new_fact.value)

            if old_norm != new_norm:
                mat = determine_materiality(field, old_fact.value, new_fact.value)
                changes.append(
                    Change(
                        field=field,
                        old=old_fact.value,
                        new=new_fact.value,
                        source_url=new_fact.source_url,
                        detected_at=new_fact.retrieved_at or datetime.now(timezone.utc),
                        materiality=mat,
                    )
                )

    return ProfileDiff(added_facts=added, removed_facts=removed, changes=changes)


def diff_profiles(old_profile: Profile, new_profile: Profile) -> ProfileDiff:
    """Compute difference between old and new company profiles."""
    return diff_facts(old_profile.facts, new_profile.facts)
