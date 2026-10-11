"""Unit tests for reconcile.py and explain.py."""

from datetime import datetime, timezone
from pathlib import Path
import pytest

from profiler.explain import (
    generate_profile_explanations,
    render_markdown_summary,
)
from profiler.models import Change, Fact, Profile
from profiler.reconcile import (
    calculate_confidence,
    reconcile_facts_with_discrepancies,
)


def test_reconcile_tier_priority():
    now = datetime.now(timezone.utc)
    fact_registry = Fact(
        orgnr="923609016",
        field="legal_name",
        value="NORWEGIAN TECH LABS AS",
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        retrieved_at=now,
        method="api",
        match_basis=["orgnr_exact"],
    )

    fact_web = Fact(
        orgnr="923609016",
        field="legal_name",
        value="Norway Tech Labs",
        source_name="Website",
        source_url="https://norwaytechlabs.no",
        retrieved_at=now,
        method="parsed",
        match_basis=["domain_match"],
    )

    facts = [fact_web, fact_registry]
    reconciled, discrepancies = reconcile_facts_with_discrepancies(facts, now=now)

    assert len(reconciled) == 1
    assert reconciled[0].value == "NORWEGIAN TECH LABS AS"
    assert len(discrepancies) == 1
    assert discrepancies[0]["field"] == "legal_name"
    assert discrepancies[0]["chosen"] == "NORWEGIAN TECH LABS AS"
    assert discrepancies[0]["rejected"] == "Norway Tech Labs"


def test_reconcile_freshness_tiebreak():
    old_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    new_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    fact_old = Fact(
        orgnr="923609016",
        field="address",
        value="Old St 1",
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/api",
        retrieved_at=old_time,
        method="api",
        match_basis=["orgnr_exact"],
    )

    fact_new = Fact(
        orgnr="923609016",
        field="address",
        value="New St 2",
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/api",
        retrieved_at=new_time,
        method="api",
        match_basis=["orgnr_exact"],
    )

    reconciled, _ = reconcile_facts_with_discrepancies([fact_old, fact_new], now=new_time)
    assert len(reconciled) == 1
    assert reconciled[0].value == "New St 2"


def test_confidence_formula():
    now = datetime.now(timezone.utc)
    fact = Fact(
        orgnr="923609016",
        field="legal_name",
        value="NORWEGIAN TECH LABS AS",
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        retrieved_at=now,
        method="api",
        match_basis=["orgnr_exact"],
    )

    score_single = calculate_confidence(fact, agreeing_sources_count=1, now=now)
    score_agreeing = calculate_confidence(fact, agreeing_sources_count=2, now=now)

    assert score_single == 1.0  # 1.0 * 1.0 * 1.0 = 1.0
    assert score_agreeing == 1.0  # Capped at 1.0


def test_explanations_only_mention_existing_facts(tmp_path: Path):
    now = datetime.now(timezone.utc)
    fact1 = Fact(
        orgnr="923609016",
        field="legal_name",
        value="NORWEGIAN TECH LABS AS",
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        retrieved_at=now,
        method="api",
        match_basis=["orgnr_exact"],
    )

    fact2 = Fact(
        orgnr="923609016",
        field="website",
        value="https://norwaytechlabs.no",
        source_name="Website",
        source_url="https://norwaytechlabs.no",
        retrieved_at=now,
        method="parsed",
        match_basis=["domain_match"],
    )

    discrepancy = {
        "field": "legal_name",
        "chosen": "NORWEGIAN TECH LABS AS",
        "rejected": "Norway Tech",
        "reason": "Registry priority",
    }

    change = Change(
        field="status",
        old="unknown",
        new="active",
        source_url="https://data.brreg.no/api",
        detected_at=now,
    )

    profile = Profile(
        orgnr="923609016",
        status="active",
        facts=[fact1, fact2],
        nulls={"financials": "no filed accounts found"},
        changes=[change],
        explanations=[],
        discrepancies=[discrepancy],
    )

    exps = generate_profile_explanations(profile)

    # 1. Matched because only lists sources present in facts
    matched = exps["matched_because"]
    assert any("Enhetsregisteret" in m for m in matched)
    assert any("Website" in m for m in matched)
    assert not any("GLEIF" in m for m in matched)

    # 2. Not found only lists null fields
    assert len(exps["not_found"]) == 1
    assert "financials: no filed accounts found" in exps["not_found"][0]

    # 3. Conflicts
    assert len(exps["conflicts"]) == 1
    assert "legal_name" in exps["conflicts"][0]

    # 4. Render Markdown summary
    summary_md = render_markdown_summary(profile, out_dir=tmp_path)
    assert "NORWEGIAN TECH LABS AS" in summary_md
    assert "https://norwaytechlabs.no" in summary_md
    assert (tmp_path / "summaries" / "923609016.md").exists()
