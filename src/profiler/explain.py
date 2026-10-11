"""Template-based fact provenance and profile summary generation module (No LLMs).

Responsibility:
- Generates template-based audit explanations for a company profile:
  * "Matched because": one line per source derived from match_basis
  * "Not found": each null field with its reason
  * "Conflicts": each discrepancy from profile.discrepancies
  * "Recently changed": from profile.changes
- Renders out/summaries/{orgnr}.md, a one-page Markdown summary with inline source links and dates.
- Ensures explanations strictly mention facts that exist in the profile.
"""

from datetime import datetime, timezone

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from profiler.models import Fact, Profile


MATCH_BASIS_DESCRIPTIONS = {
    "orgnr_exact": "Matched via exact organisation number (orgnr_exact)",
    "orgnr_on_page": "Matched via 9-digit organisation number on webpage text (orgnr_on_page)",
    "wikidata_property_p2333": "Matched via Wikidata Property P2333 (wikidata_property_p2333)",
    "supplier_orgnum": "Matched via Doffin supplier organisation number (supplier_orgnum)",
    "domain_match": "Matched via official registry domain fact (domain_match)",
    "redirected_domain": "Matched via domain redirect (redirected_domain)",
}


def explain_fact(fact: Fact) -> str:
    """Generate template-based explanation for an individual fact."""
    mb_str = ", ".join(fact.match_basis) if fact.match_basis else "unspecified"
    date_str = fact.retrieved_at.strftime("%Y-%m-%d") if fact.retrieved_at else "unknown"
    return f"{fact.source_name}: Provided '{fact.field}' on {date_str} (match: {mb_str})"


def generate_profile_explanations(profile: Profile) -> Dict[str, List[str]]:
    """Generate template-based audit explanations for a company profile.

    Categories:
    - matched_because: one line per source derived from match_basis
    - not_found: each null field with its reason
    - conflicts: each discrepancy
    - recently_changed: from changes[]
    """
    explanations: Dict[str, List[str]] = {
        "matched_because": [],
        "not_found": [],
        "conflicts": [],
        "recently_changed": [],
    }

    # 1. Matched because: group facts by source_name
    sources_match: Dict[str, set] = {}
    for fact in profile.facts:
        if fact.source_name:
            sources_match.setdefault(fact.source_name, set()).update(fact.match_basis)

    for source_name, match_bases in sources_match.items():
        mb_labels = [MATCH_BASIS_DESCRIPTIONS.get(mb, mb) for mb in sorted(match_bases)]
        mb_text = "; ".join(mb_labels) if mb_labels else "exact match"
        explanations["matched_because"].append(f"{source_name}: {mb_text}")

    # 2. Not found: null fields
    for field_name, reason in sorted(profile.nulls.items()):
        explanations["not_found"].append(f"{field_name}: {reason}")

    # 3. Conflicts: discrepancies
    for disc in profile.discrepancies:
        if isinstance(disc, dict):
            f_name = disc.get("field", "unknown")
            chosen = disc.get("chosen")
            rejected = disc.get("rejected")
            reason = disc.get("reason", "")
            explanations["conflicts"].append(f"{f_name}: chosen '{chosen}' vs rejected '{rejected}' ({reason})")
        else:
            explanations["conflicts"].append(str(disc))

    # 4. Recently changed: changes
    for change in profile.changes:
        f_name = getattr(change, "field", change.get("field", "unknown") if isinstance(change, dict) else "unknown")
        old_val = getattr(change, "old", change.get("old", None) if isinstance(change, dict) else None)
        new_val = getattr(change, "new", change.get("new", None) if isinstance(change, dict) else None)
        explanations["recently_changed"].append(f"{f_name}: changed from '{old_val}' to '{new_val}'")

    return explanations


def render_markdown_summary(profile: Profile, out_dir: Optional[Path] = None) -> str:
    """Render out/summaries/{orgnr}.md one-page markdown summary with inline source links and dates."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    explanations = generate_profile_explanations(profile)

    # Extract primary company attributes from facts
    name_fact = next((f for f in profile.facts if f.field in ("name", "legal_name")), None)
    company_name = str(name_fact.value) if name_fact else f"Organisation {profile.orgnr}"

    lines = []
    lines.append(f"# Company Summary: {company_name}")
    lines.append(f"**Organisation Number**: `{profile.orgnr}` | **Status**: `{profile.status.upper()}` | **Generated**: {now_str}")
    lines.append("")

    lines.append("## Published Facts & Provenance")
    if profile.facts:
        lines.append("| Field | Value | Source & Date | Confidence | Match Basis |")
        lines.append("|---|---|---|---|---|")
        for fact in profile.facts:
            val_display = str(fact.value)
            if len(val_display) > 80:
                val_display = val_display[:77] + "..."
            val_display = val_display.replace("\n", " ")

            date_str = fact.retrieved_at.strftime("%Y-%m-%d") if fact.retrieved_at else "N/A"
            source_link = f"[{fact.source_name}]({fact.source_url})" if fact.source_url else fact.source_name
            mb_str = ", ".join(fact.match_basis)
            lines.append(f"| `{fact.field}` | {val_display} | {source_link} ({date_str}) | `{fact.confidence:.2f}` | `{mb_str}` |")
    else:
        lines.append("*No published facts recorded for this entity.*")

    lines.append("")
    lines.append("## Audit & Provenance Explanations")
    lines.append("")

    lines.append("### Matched Because")
    if explanations["matched_because"]:
        for item in explanations["matched_because"]:
            lines.append(f"- {item}")
    else:
        lines.append("- *No source matches recorded.*")

    lines.append("")
    lines.append("### Not Found")
    if explanations["not_found"]:
        for item in explanations["not_found"]:
            lines.append(f"- {item}")
    else:
        lines.append("- *All expected fields were successfully populated.*")

    lines.append("")
    lines.append("### Conflicts & Discrepancies")
    if explanations["conflicts"]:
        for item in explanations["conflicts"]:
            lines.append(f"- ⚠️ {item}")
    else:
        lines.append("- *No cross-source discrepancies detected.*")

    lines.append("")
    lines.append("### Recently Changed")
    if explanations["recently_changed"]:
        for item in explanations["recently_changed"]:
            lines.append(f"- 🔄 {item}")
    else:
        lines.append("- *No recent attribute changes detected.*")

    markdown_content = "\n".join(lines) + "\n"

    if out_dir:
        summaries_dir = out_dir / "summaries"
        summaries_dir.mkdir(parents=True, exist_ok=True)
        summary_file = summaries_dir / f"{profile.orgnr}.md"
        summary_file.write_text(markdown_content, encoding="utf-8")

    return markdown_content


def audit_profile_provenance(profile: Profile) -> Dict[str, Any]:
    """Generate comprehensive provenance report for a Profile."""
    exps = generate_profile_explanations(profile)
    return {
        "orgnr": profile.orgnr,
        "status": profile.status,
        "total_published_facts": len(profile.facts),
        "fact_sources": list(set(f.source_url for f in profile.facts if f.source_url)),
        "methods_used": list(set(f.method for f in profile.facts)),
        "match_bases": list(set(mb for f in profile.facts for mb in f.match_basis)),
        "null_fields": list(profile.nulls.keys()),
        "explanations": exps,
    }
