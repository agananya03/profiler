"""Adversarial tests for prompt injection, invalid orgnrs, name-only spoofing, hallucinated quotes, and crossover resolution."""

from datetime import datetime, timezone
import pytest
from profiler.llm import validate_llm_fact
from profiler.models import Fact, clean_orgnr
from profiler.resolve import Decision, accept


class MockCtx:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_adversarial_invalid_orgnrs():
    invalid_inputs = [
        "12345",
        "ABCDEFGHI",
        "92360901",  # 8 digits
        "9236090160",  # 10 digits
        "'; DROP TABLE profiles; --",
    ]
    for inp in invalid_inputs:
        with pytest.raises(ValueError):
            clean_orgnr(inp)


def test_adversarial_hallucinated_verbatim_quote():
    page_text = "Acme AS was registered in Oslo in 2020."
    quote = "Acme AS is a market leader in AI software."  # Hallucinated by LLM
    value = "AI software"

    # Rule 4 MUST reject hallucinated quote
    assert validate_llm_fact("description", value, quote, page_text) is False


def test_adversarial_hallucinated_financials_from_llm():
    page_text = "Acme AS generated revenue of 5000000 NOK."
    quote = "Acme AS generated revenue of 5000000 NOK."
    value = 5000000.0

    # Rule 3 MUST reject LLM extracted financials
    assert validate_llm_fact("revenue", value, quote, page_text) is False


def test_adversarial_two_companies_same_name():
    # Two companies with the same name: Company A (923609016) vs Company B (986161474)
    now = datetime.now(timezone.utc)

    fact_b = Fact(
        orgnr="986161474",
        field="employee_count",
        value=500,
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/986161474",
        retrieved_at=now,
        method="api",
        match_basis=["orgnr_exact"],
    )

    ctx_a = MockCtx(
        orgnr="923609016",
        registry_facts={"legal_name": "NORDIC TECH AS", "postal_code": "0155"},
        page_text="NORDIC TECH AS was founded in Oslo.",
    )

    decision = accept(fact_b, ctx_a)
    assert decision.accepted is False
    assert "Crossover" in decision.reason or "does not match" in decision.reason


def test_adversarial_parent_vs_subsidiary_consolidated_accounts():
    now = datetime.now(timezone.utc)

    # Consolidated financial statement on a subsidiary (non-parent) entity
    subsidiary_fact = Fact(
        orgnr="991272658",
        field="financials",
        value={"year": 2023, "revenue": 50000000.0, "is_consolidated": True},
        source_name="Regnskapsregisteret",
        source_url="https://data.brreg.no/regnskapsregisteret/regnskap/991272658",
        retrieved_at=now,
        method="api",
        match_basis=["orgnr_exact"],
    )

    ctx_sub = MockCtx(
        orgnr="991272658",
        is_parent=False,  # Subsidiary is not a parent
        registry_facts={"legal_name": "SUBSIDIARY AS"},
    )

    decision = accept(subsidiary_fact, ctx_sub)
    assert decision.accepted is False
    assert "Rule 6" in decision.reason


def test_adversarial_branch_vs_main_unit_crossover():
    now = datetime.now(timezone.utc)

    # Branch fact (933489272) attempted to be assigned to Main Unit (923609016)
    branch_fact = Fact(
        orgnr="933489272",
        field="business_address",
        value={"adresse": ["Branch Road 5"], "postnummer": "6652"},
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/underenheter/933489272",
        retrieved_at=now,
        method="api",
        match_basis=["orgnr_exact"],
    )

    ctx_main = MockCtx(
        orgnr="923609016",
        registry_facts={"legal_name": "MAIN UNIT AS"},
    )

    decision = accept(branch_fact, ctx_main)
    assert decision.accepted is False
    assert "Crossover" in decision.reason or "does not match" in decision.reason


def test_adversarial_website_similarly_named_company():
    now = datetime.now(timezone.utc)

    # Scraped webpage from "nordic-tech-solutions.no" when profiling "NORDIC TECH AS" (923609016)
    website_fact = Fact(
        orgnr="923609016",
        field="description",
        value="Leading provider of IT services.",
        source_name="Website",
        source_url="https://nordic-tech-solutions.no/about",
        retrieved_at=now,
        method="parsed",
        match_basis=["website_scrape"],
    )

    ctx = MockCtx(
        orgnr="923609016",
        official_website="www.nordictech.no",
        legal_name="NORDIC TECH AS",
        postal_code="0155",
        page_text="Nordic Tech Solutions AS provides software engineering across Scandinavia.",
    )

    decision = accept(website_fact, ctx)
    assert decision.accepted is False
    assert "Rule 2" in decision.reason


def test_adversarial_page_mentions_different_company_orgnr():
    now = datetime.now(timezone.utc)

    # Web page for profiling Company A (923609016) that mentions Company B's orgnr (986161474)
    scraped_fact = Fact(
        orgnr="923609016",
        field="partner_info",
        value="Partnered with Company B",
        source_name="Web_Scraper",
        source_url="https://tech-news.no/article",
        retrieved_at=now,
        method="parsed",
        match_basis=["text_mention"],
    )

    ctx = MockCtx(
        orgnr="923609016",
        official_website="www.company-a.no",
        legal_name="COMPANY A AS",
        postal_code="0155",
        page_text="Company B org nr 986161474 announced a new partnership today.",
    )

    decision = accept(scraped_fact, ctx)
    assert decision.accepted is False
    assert "Rule 2" in decision.reason or "Rule 3" in decision.reason
