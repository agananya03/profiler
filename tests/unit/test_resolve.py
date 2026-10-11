"""Unit tests for resolve.py publication gate and resolution rules 1-6."""

from datetime import datetime, timezone
import pytest
from profiler.models import Fact, normalize_url
from profiler.resolve import Decision, _extract_domain, accept


class MockCtx:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_rule1_official_api_accepted():
    now = datetime.now(timezone.utc)
    fact = Fact(
        orgnr="923609016",
        field="name",
        value="EQUINOR ASA",
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        retrieved_at=now,
        method="api",
        match_basis=["orgnr_exact"],
    )

    decision = accept(fact)
    assert decision.accepted is True
    assert "Rule 1" in decision.reason


def test_rule2_website_fact_official_domain():
    now = datetime.now(timezone.utc)
    fact = Fact(
        orgnr="923609016",
        field="description",
        value="Energy company",
        source_name="Website",
        source_url="https://www.equinor.com/about",
        retrieved_at=now,
        method="parsed",
        match_basis=["website_scrape"],
    )

    ctx = MockCtx(
        orgnr="923609016",
        official_website="www.equinor.com",
        page_text="Equinor is an energy company.",
    )

    decision = accept(fact, ctx)
    assert decision.accepted is True


def test_rule2_website_fact_rejected():
    now = datetime.now(timezone.utc)
    fact = Fact(
        orgnr="923609016",
        field="description",
        value="Fake info",
        source_name="Website",
        source_url="https://unrelated-domain.com/page",
        retrieved_at=now,
        method="parsed",
        match_basis=["website_scrape"],
    )

    ctx = MockCtx(
        orgnr="923609016",
        official_website="www.equinor.com",
        page_text="This page does not contain target orgnr.",
    )

    decision = accept(fact, ctx)
    assert decision.accepted is False
    assert "Rule 2" in decision.reason


def test_rule4_name_only_always_rejected():
    now = datetime.now(timezone.utc)
    fact = Fact.model_construct(
        orgnr="923609016",
        field="employee_count",
        value=100,
        source_name="Secondary",
        source_url="https://example.com",
        retrieved_at=now,
        method="parsed",
        match_basis=["name_only"],
    )

    decision = accept(fact)
    assert decision.accepted is False
    assert "Rule 4" in decision.reason


def test_rule5_registry_contradiction_rejected():
    now = datetime.now(timezone.utc)
    fact = Fact(
        orgnr="923609016",
        field="status",
        value="inactive",
        source_name="Web_Scraper",
        source_url="https://example.com",
        retrieved_at=now,
        method="parsed",
        match_basis=["orgnr_exact"],
    )

    ctx = MockCtx(
        orgnr="923609016",
        registry_facts={"status": "active"},
    )

    decision = accept(fact, ctx)
    assert decision.accepted is False
    assert "Rule 5" in decision.reason


def test_rule6_consolidated_accounts_on_non_parent_rejected():
    now = datetime.now(timezone.utc)
    fact = Fact(
        orgnr="991272658",
        field="financials",
        value={"year": 2023, "revenue": 1000000.0, "is_consolidated": True},
        source_name="Regnskapsregisteret",
        source_url="https://data.brreg.no/regnskapsregisteret/regnskap/991272658",
        retrieved_at=now,
        method="api",
        match_basis=["orgnr_exact"],
    )

    ctx = MockCtx(
        orgnr="991272658",
        is_parent=False,
    )

    decision = accept(fact, ctx)
    assert decision.accepted is False
    assert "Rule 6" in decision.reason


def test_postboks_postal_address_and_street_business_address_accepted():
    now = datetime.now(timezone.utc)
    street_bus_addr = Fact(
        orgnr="971526920",
        field="business_address",
        value={"adresse": ["Storgata 1"], "postnummer": "0155", "poststed": "OSLO"},
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/971526920",
        retrieved_at=now,
        method="api",
        match_basis=["orgnr_exact"],
    )
    postboks_post_addr = Fact(
        orgnr="971526920",
        field="postal_address",
        value={"adresse": ["Postboks 2633 St. Hanshaugen"], "postnummer": "0301", "poststed": "OSLO"},
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/971526920",
        retrieved_at=now,
        method="api",
        match_basis=["orgnr_exact"],
    )

    ctx = MockCtx(
        orgnr="971526920",
        registry_facts={
            "business_address": street_bus_addr.value,
            "postal_address": postboks_post_addr.value,
        },
    )

    dec_bus = accept(street_bus_addr, ctx)
    dec_post = accept(postboks_post_addr, ctx)

    assert dec_bus.accepted is True
    assert dec_post.accepted is True


def test_normalize_url_and_domain_extraction():
    assert normalize_url("[www.equinor.com](https://www.equinor.com)") == "https://www.equinor.com"
    assert normalize_url("www.equinor.com") == "https://www.equinor.com"

    assert _extract_domain("[www.equinor.com](https://www.equinor.com)") == "equinor.com"
    assert _extract_domain("https://www.norgesbank.no/") == "norgesbank.no"
    assert _extract_domain("https://www.ssb.no") == "ssb.no"
    assert _extract_domain("https://www.mil.no/en/about-us/museums-and-music") == "mil.no"
    assert _extract_domain("https://equinor-careers.com/jobs") == "equinor-careers.com"


def test_domain_matching_equinor_norgesbank_ssb_forsvaret_and_careers_rejection():
    now = datetime.now(timezone.utc)

    # 1. Equinor pass
    ctx_equinor = MockCtx(orgnr="923609016", registry_facts={"website": "[www.equinor.com](https://www.equinor.com)"})
    fact_equinor = Fact(
        orgnr="923609016",
        field="emails",
        value=["info@equinor.com"],
        source_name="Website",
        source_url="https://www.equinor.com/about",
        retrieved_at=now,
        method="parsed",
        match_basis=["domain_match"],
    )
    assert accept(fact_equinor, ctx_equinor).accepted is True

    # 2. Norges Bank pass
    ctx_nb = MockCtx(orgnr="937884117", registry_facts={"website": "www.norgesbank.no"})
    fact_nb = Fact(
        orgnr="937884117",
        field="emails",
        value=["kontakt@norgesbank.no"],
        source_name="Website",
        source_url="https://www.norgesbank.no/en/topics/",
        retrieved_at=now,
        method="parsed",
        match_basis=["domain_match"],
    )
    assert accept(fact_nb, ctx_nb).accepted is True

    # 3. SSB pass
    ctx_ssb = MockCtx(orgnr="971526920", registry_facts={"website": "https://www.ssb.no"})
    fact_ssb = Fact(
        orgnr="971526920",
        field="emails",
        value=["ssb@ssb.no"],
        source_name="Website",
        source_url="https://www.ssb.no/en/statbank",
        retrieved_at=now,
        method="parsed",
        match_basis=["domain_match"],
    )
    assert accept(fact_ssb, ctx_ssb).accepted is True

    # 4. Forsvaret pass
    ctx_mil = MockCtx(orgnr="976037286", registry_facts={"website": "www.mil.no"})
    fact_mil = Fact(
        orgnr="976037286",
        field="emails",
        value=["post@mil.no"],
        source_name="Website",
        source_url="https://www.mil.no/en/about-us/museums-and-music",
        retrieved_at=now,
        method="parsed",
        match_basis=["domain_match"],
    )
    assert accept(fact_mil, ctx_mil).accepted is True

    # 5. equinor-careers.com rejected
    fact_careers = Fact(
        orgnr="923609016",
        field="emails",
        value=["jobs@equinor-careers.com"],
        source_name="Website",
        source_url="https://equinor-careers.com/jobs",
        retrieved_at=now,
        method="parsed",
        match_basis=["domain_match"],
    )
    assert accept(fact_careers, ctx_equinor).accepted is False

