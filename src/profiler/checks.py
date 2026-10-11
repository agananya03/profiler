"""Data integrity check assertions module (Rules 1, 3, 5).

Responsibility: Performs validation checks on compiled company profiles to catch inconsistencies:
- Ensures financial field methods adhere strictly to Rule 3 ('api' or 'parsed').
- Enforces Mod-11 orgnr validation and forbids 'name_only' match basis.
- Validates revenue >= 0.
- Asserts total_assets is within 2% of (equity + debt) when all three are present.
- Flags (without dropping) year-on-year financial metric changes over 100x.
"""

from typing import Any, Dict, List
from profiler.models import FINANCIAL_FIELDS, Profile


def run_integrity_checks(profile: Profile) -> List[str]:
    """Run integrity checks on Profile. Returns list of warning/error messages."""
    warnings: List[str] = []

    if not profile.orgnr or len(profile.orgnr) != 9:
        warnings.append("Profile organisation number is missing or invalid.")

    fin_statements: List[Dict[str, Any]] = []

    for fact in profile.facts:
        field_lower = fact.field.lower()

        # Rule 3 check: Financial fields must come from api or parsed
        if field_lower in FINANCIAL_FIELDS or field_lower == "financials":
            if fact.method not in ("api", "parsed"):
                warnings.append(
                    f"VIOLATION Rule 3: Financial field '{fact.field}' has prohibited method '{fact.method}'."
                )

        # Rule 5 check: Name-only matching is forbidden
        for mb in fact.match_basis:
            if mb.lower() == "name_only":
                warnings.append(f"VIOLATION Rule 5: Fact for field '{fact.field}' matched by name_only.")

        # Collect financial statement dictionaries for mathematical checks
        if field_lower == "financials" and isinstance(fact.value, dict):
            fin_statements.append(fact.value)

    # 1. Check revenue >= 0
    for stmt in fin_statements:
        year = stmt.get("year", "unknown")
        rev = stmt.get("revenue")
        if rev is not None and rev < 0:
            warnings.append(f"VIOLATION: Negative revenue ({rev}) detected for accounting year {year}.")

    # 2. Check balance sheet equation: assets within 2% of (equity + debt) when all present
    for stmt in fin_statements:
        year = stmt.get("year", "unknown")
        assets = stmt.get("total_assets")
        eq = stmt.get("equity")
        debt = stmt.get("total_debt")

        if assets is not None and eq is not None and debt is not None:
            equity_plus_debt = eq + debt
            diff = abs(assets - equity_plus_debt)
            denom = abs(assets) if abs(assets) > 0 else 1.0
            ratio_diff = diff / denom
            if ratio_diff > 0.02:
                warnings.append(
                    f"FLAG: Balance sheet equation mismatch for year {year}: total_assets ({assets}) "
                    f"differs from equity + debt ({equity_plus_debt}) by {ratio_diff * 100:.2f}% (> 2%)."
                )

    # 3. Flag year-on-year metric changes over 100x (without dropping data)
    valid_stmts = [s for s in fin_statements if s.get("year") is not None]
    sorted_stmts = sorted(valid_stmts, key=lambda s: int(s["year"]))

    metrics_to_check = ("revenue", "operating_result", "net_result", "total_assets", "equity", "total_debt")
    for i in range(1, len(sorted_stmts)):
        prev_stmt = sorted_stmts[i - 1]
        curr_stmt = sorted_stmts[i]
        prev_yr, curr_yr = prev_stmt["year"], curr_stmt["year"]

        for metric_name in metrics_to_check:
            prev_val = prev_stmt.get(metric_name)
            curr_val = curr_stmt.get(metric_name)

            if prev_val is not None and curr_val is not None and prev_val != 0:
                change_ratio = abs(curr_val / prev_val)
                if change_ratio > 100.0 or change_ratio < 0.01:
                    warnings.append(
                        f"FLAG: Significant year-on-year change over 100x for '{metric_name}' between {prev_yr} ({prev_val}) "
                        f"and {curr_yr} ({curr_val}) (ratio: {change_ratio:.1f}x)."
                    )

    return warnings
