"""Unit tests for WebsiteAdapter deterministic website extraction."""

from pathlib import Path
import pytest
import respx

from profiler.adapters.base import Ctx
from profiler.adapters.website import WebsiteAdapter
from profiler.budget import BudgetTracker
from profiler.http import HttpClient

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def website_html_fixture() -> str:
    return (FIXTURES_DIR / "sample_company_website.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
@respx.mock
async def test_website_adapter_deterministic_extraction(website_html_fixture: str):
    start_url = "https://norwaytechlabs.no"
    robots_url = "https://norwaytechlabs.no/robots.txt"

    respx.get(robots_url).respond(status_code=200, text="User-agent: *\nAllow: /")
    respx.get(start_url).respond(status_code=200, text=website_html_fixture)

    # Internal page mocks
    respx.get("https://norwaytechlabs.no/om-oss").respond(status_code=404)
    respx.get("https://norwaytechlabs.no/kontakt").respond(status_code=404)
    respx.get("https://norwaytechlabs.no/tjenester").respond(status_code=404)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = WebsiteAdapter(target_url=start_url)
        facts = await adapter.fetch("923609016", ctx)

        assert len(facts) > 0

        # Check emails
        email_fact = next(f for f in facts if f.field == "emails")
        assert "info@norwaytechlabs.no" in email_fact.value
        assert email_fact.content_hash is not None
        assert "orgnr_on_page" in email_fact.match_basis

        # Check phone numbers
        phone_fact = next(f for f in facts if f.field == "phone_numbers")
        assert "+47 22 33 44 55" in phone_fact.value

        # Check social links
        social_fact = next(f for f in facts if f.field == "social_links")
        assert "linkedin" in social_fact.value
        assert "facebook" in social_fact.value

        # Check Schema.org JSON-LD
        schema_desc = next(f for f in facts if f.field == "schema_description")
        assert "Enterprise cloud software" in schema_desc.value

        schema_logo = next(f for f in facts if f.field == "schema_logo")
        assert "logo.png" in schema_logo.value


@pytest.mark.asyncio
@respx.mock
async def test_website_adapter_robots_disallowed(website_html_fixture: str):
    start_url = "https://norwaytechlabs.no"
    robots_url = "https://norwaytechlabs.no/robots.txt"

    respx.get(robots_url).respond(status_code=200, text="User-agent: *\nDisallow: /")
    respx.get(start_url).respond(status_code=200, text=website_html_fixture)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = WebsiteAdapter(target_url=start_url)
        facts = await adapter.fetch("923609016", ctx)

        # Disallowed by robots.txt
        assert len(facts) == 0


@pytest.mark.asyncio
@respx.mock
async def test_website_adapter_redirect_different_domain():
    start_url = "https://old-domain.no"
    final_url = "https://new-domain.com"
    robots_url = "https://old-domain.no/robots.txt"

    respx.get(robots_url).respond(status_code=200, text="User-agent: *\nAllow: /")
    respx.get(start_url).respond(
        status_code=200,
        text="<html><body><p>Org.nr: 923 609 016</p><a href='mailto:info@new-domain.com'>info@new-domain.com</a></body></html>",
        headers={"Location": final_url},
    )

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = WebsiteAdapter(target_url=start_url)
        facts = await adapter.fetch("923609016", ctx)

        assert len(facts) > 0
        email_fact = next(f for f in facts if f.field == "emails")
        assert "info@new-domain.com" in email_fact.value


@pytest.mark.asyncio
@respx.mock
async def test_website_adapter_registry_fact_match_basis(website_html_fixture: str):
    start_url = "https://norwaytechlabs.no"
    robots_url = "https://norwaytechlabs.no/robots.txt"

    respx.get(robots_url).respond(status_code=200, text="User-agent: *\nAllow: /")
    respx.get(start_url).respond(status_code=200, text=website_html_fixture)

    respx.get("https://norwaytechlabs.no/om-oss").respond(status_code=404)
    respx.get("https://norwaytechlabs.no/kontakt").respond(status_code=404)
    respx.get("https://norwaytechlabs.no/tjenester").respond(status_code=404)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        setattr(ctx, "registry_facts", {"website": "[norwaytechlabs.no](https://norwaytechlabs.no)"})
        adapter = WebsiteAdapter()
        facts = await adapter.fetch("923609016", ctx)

        assert len(facts) > 0
        # clean_page_text must NOT be in published facts
        assert not any(f.field == "clean_page_text" for f in facts)
        # clean_page_text MUST be in pipeline context ctx
        assert getattr(ctx, "clean_page_text", None) is not None

        email_fact = next(f for f in facts if f.field == "emails")
        assert email_fact.match_basis == ["orgnr_exact", "domain_from_brreg"]
