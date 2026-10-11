"""Unit tests for store.py, diff.py, and update change tracking."""

from datetime import datetime, timezone
import pytest

from profiler.diff import determine_materiality, diff_facts, diff_profiles
from profiler.models import Fact, Profile
from profiler.store import Storage


def test_sqlite_storage_upsert_and_history(tmp_path):
    db_file = tmp_path / "test_store.db"
    store = Storage(db_path=str(db_file))

    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    f1 = Fact(
        orgnr="923609016",
        field="legal_name",
        value="NORWEGIAN TECH LABS AS",
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/api",
        retrieved_at=t1,
        method="api",
        match_basis=["orgnr_exact"],
    )

    # First upsert -> facts_current
    store.upsert_fact(f1)
    current_facts = store.get_current_facts("923609016")
    assert len(current_facts) == 1
    assert current_facts[0].value == "NORWEGIAN TECH LABS AS"
    assert len(store.get_fact_history("923609016")) == 0

    # Mutate value & second upsert -> archives old into fact_history
    t2 = datetime(2026, 2, 1, tzinfo=timezone.utc)
    f2 = Fact(
        orgnr="923609016",
        field="legal_name",
        value="EQUINOR TECH AS",
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/api",
        retrieved_at=t2,
        method="api",
        match_basis=["orgnr_exact"],
    )

    store.upsert_fact(f2)
    current_facts_updated = store.get_current_facts("923609016")
    assert len(current_facts_updated) == 1
    assert current_facts_updated[0].value == "EQUINOR TECH AS"

    history = store.get_fact_history("923609016", field="legal_name")
    assert len(history) == 1
    assert history[0]["value"] == "NORWEGIAN TECH LABS AS"

    store.close()


def test_materiality_ranking():
    assert determine_materiality("is_bankrupt", False, True) == "high"
    assert determine_materiality("under_liquidation", False, True) == "high"
    assert determine_materiality("announcement_merger", [], ["merger notice"]) == "high"
    assert determine_materiality("ceo", "Old CEO", "New CEO") == "high"
    assert determine_materiality("status", "active", "inactive") == "high"

    # Revenue drop over 30% -> medium
    assert determine_materiality("revenue", 1000000.0, 500000.0) == "medium"

    # Address / Name change -> medium
    assert determine_materiality("legal_name", "Old Name AS", "New Name AS") == "medium"
    assert determine_materiality("registered_address", "Street 1", "Street 2") == "medium"

    # Minor field -> low
    assert determine_materiality("website", "https://old.no", "https://new.no") == "low"


def test_double_run_mutate_fixture_detects_changes():
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 2, 1, tzinfo=timezone.utc)

    # Run 1 Profile
    fact_name_1 = Fact(
        orgnr="923609016",
        field="legal_name",
        value="NORWEGIAN TECH LABS AS",
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/api/enheter/923609016",
        retrieved_at=t1,
        method="api",
        match_basis=["orgnr_exact"],
    )

    fact_ceo_1 = Fact(
        orgnr="923609016",
        field="ceo",
        value="Ola Nordmann",
        source_name="Roller",
        source_url="https://data.brreg.no/api/enheter/923609016/roller",
        retrieved_at=t1,
        method="api",
        match_basis=["orgnr_exact"],
    )

    fact_rev_1 = Fact(
        orgnr="923609016",
        field="revenue",
        value=10000000.0,
        source_name="Regnskapsregisteret",
        source_url="https://data.brreg.no/api/regnskap/923609016",
        retrieved_at=t1,
        method="api",
        match_basis=["orgnr_exact"],
    )

    old_profile = Profile(
        orgnr="923609016",
        status="active",
        facts=[fact_name_1, fact_ceo_1, fact_rev_1],
    )

    # Run 2 Mutated Profile (Name change, New CEO, 50% Revenue drop)
    fact_name_2 = Fact(
        orgnr="923609016",
        field="legal_name",
        value="EQUINOR TECH LABS AS",
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/api/enheter/923609016",
        retrieved_at=t2,
        method="api",
        match_basis=["orgnr_exact"],
    )

    fact_ceo_2 = Fact(
        orgnr="923609016",
        field="ceo",
        value="Kari Nordmann",
        source_name="Roller",
        source_url="https://data.brreg.no/api/enheter/923609016/roller",
        retrieved_at=t2,
        method="api",
        match_basis=["orgnr_exact"],
    )

    fact_rev_2 = Fact(
        orgnr="923609016",
        field="revenue",
        value=5000000.0,  # 50% drop
        source_name="Regnskapsregisteret",
        source_url="https://data.brreg.no/api/regnskap/923609016",
        retrieved_at=t2,
        method="api",
        match_basis=["orgnr_exact"],
    )

    new_profile = Profile(
        orgnr="923609016",
        status="active",
        facts=[fact_name_2, fact_ceo_2, fact_rev_2],
    )

    diff_res = diff_profiles(old_profile, new_profile)
    assert diff_res.has_changes() is True
    assert len(diff_res.changes) == 3

    # Assert old, new, source_url, detected_at, and materiality
    name_change = next(c for c in diff_res.changes if c.field == "legal_name")
    assert name_change.old == "NORWEGIAN TECH LABS AS"
    assert name_change.new == "EQUINOR TECH LABS AS"
    assert name_change.source_url == "https://data.brreg.no/api/enheter/923609016"
    assert name_change.detected_at == t2
    assert name_change.materiality == "medium"

    ceo_change = next(c for c in diff_res.changes if c.field == "ceo")
    assert ceo_change.old == "Ola Nordmann"
    assert ceo_change.new == "Kari Nordmann"
    assert ceo_change.source_url == "https://data.brreg.no/api/enheter/923609016/roller"
    assert ceo_change.detected_at == t2
    assert ceo_change.materiality == "high"

    rev_change = next(c for c in diff_res.changes if c.field == "revenue")
    assert rev_change.old == 10000000.0
    assert rev_change.new == 5000000.0
    assert rev_change.materiality == "medium"
