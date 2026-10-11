"""Unit tests for validate.py and models.py (Mod-11, Fact, Change, Profile)."""

from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from profiler.models import Change, Fact, Profile
from profiler.validate import normalize_orgnr, valid_orgnr


def test_normalize_orgnr():
    assert normalize_orgnr("923 609 016") == "923609016"
    assert normalize_orgnr("  923  609  016  ") == "923609016"
    assert normalize_orgnr("923-609-016") == "923609016"

    with pytest.raises(ValueError):
        normalize_orgnr("12345")

    with pytest.raises(ValueError):
        normalize_orgnr(123456789)  # Non-string raises ValueError


def test_valid_orgnr_mod11():
    # 923609016: 9*3 + 2*2 + 3*7 + 6*6 + 0*5 + 9*4 + 0*3 + 1*2 = 27+4+21+36+0+36+0+2 = 126.
    # 126 % 11 = 5. Check digit = 11 - 5 = 6. Last digit is 6 -> VALID.
    assert valid_orgnr("923 609 016") is True
    assert valid_orgnr("923609016") is True

    # Invalid check digit
    assert valid_orgnr("923609015") is False

    # Invalid length or non-numeric
    assert valid_orgnr("1234") is False
    assert valid_orgnr("ABCDEFGHI") is False


def test_valid_orgnr_remainder_1_invalid():
    # Test cases where Mod-11 remainder is 1 (resulting in check digit 10, which is invalid)
    # 123456783: 1*3+2*2+3*7+4*6+5*5+6*4+7*3+8*2 = 3+4+21+24+25+24+21+16 = 138.
    # 138 % 11 = 6. Check digit = 11-6 = 5. Last digit is 3 != 5 -> False.
    assert valid_orgnr("123456789") is False


def test_fact_valid_construction():
    now = datetime.now(timezone.utc)
    fact = Fact(
        orgnr="923609016",
        field="name",
        value="NORWEGIAN TECH LABS AS",
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        retrieved_at=now,
        method="api",
        match_basis=["orgnr_exact_api_endpoint"],
    )
    assert fact.orgnr == "923609016"
    assert fact.source_url != ""
    assert fact.content_hash is not None


def test_fact_missing_evidence_quote_for_llm_verified_invalid():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError, match="evidence_quote is required when method is 'llm_verified'"):
        Fact(
            orgnr="923609016",
            field="description",
            value="Enterprise cloud software",
            source_name="LLM Website Scraper",
            source_url="https://norwaytechlabs.no",
            retrieved_at=now,
            method="llm_verified",
            match_basis=["orgnr_in_html_body"],
            evidence_quote=None,  # Missing evidence quote
        )


def test_fact_llm_verified_financial_field_invalid():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError, match="is financial and requires method 'api' or 'parsed'"):
        Fact(
            orgnr="923609016",
            field="revenue",
            value=15000000.0,
            source_name="LLM Website Scraper",
            source_url="https://norwaytechlabs.no",
            retrieved_at=now,
            method="llm_verified",
            match_basis=["orgnr_in_html_body"],
            evidence_quote="Revenue was 15000000 NOK in 2023.",
        )


def test_fact_financial_field_with_api_method_valid():
    now = datetime.now(timezone.utc)
    fact = Fact(
        orgnr="923609016",
        field="revenue",
        value=15000000.0,
        source_name="Regnskapsregisteret",
        source_url="https://data.brreg.no/regnskapsregisteret/regnskap/923609016",
        retrieved_at=now,
        method="api",
        match_basis=["orgnr_exact_api_endpoint"],
    )
    assert fact.field == "revenue"
    assert fact.method == "api"


def test_fact_missing_source_url_invalid():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        Fact(
            orgnr="923609016",
            field="name",
            value="TEST",
            source_name="Enhetsregisteret",
            source_url="",  # Empty string rejected
            retrieved_at=now,
            method="api",
        )


def test_change_and_profile_construction():
    now = datetime.now(timezone.utc)
    change = Change(
        field="employee_count",
        old=10,
        new=15,
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        detected_at=now,
        materiality="low",
    )
    assert change.field == "employee_count"

    profile = Profile(
        orgnr="923 609 016",
        status="active",
        changes=[change],
        nulls={"financials": "no filed accounts found"},
    )
    assert profile.orgnr == "923609016"
    assert profile.status == "active"
    assert profile.nulls["financials"] == "no filed accounts found"


def test_profile_invalid_status():
    with pytest.raises(ValidationError):
        Profile(
            orgnr="923609016",
            status="bankrupt_invalid",  # Invalid status choice
        )
