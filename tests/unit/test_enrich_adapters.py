"""Offline unit tests for GLEIF, Wikidata, and Doffin adapters using respx and recorded fixtures."""

from pathlib import Path
import pytest
import respx

from profiler.adapters.base import Ctx
from profiler.adapters.doffin import DoffinAdapter
from profiler.adapters.gleif import GleifAdapter
from profiler.adapters.wikidata import WikidataAdapter
from profiler.budget import BudgetTracker
from profiler.http import HttpClient

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def gleif_fixture() -> str:
    return (FIXTURES_DIR / "gleif_923609016.json").read_text(encoding="utf-8")


@pytest.fixture
def wikidata_fixture() -> str:
    return (FIXTURES_DIR / "wikidata_923609016.json").read_text(encoding="utf-8")


@pytest.fixture
def doffin_fixture() -> str:
    return (FIXTURES_DIR / "doffin_923609016.json").read_text(encoding="utf-8")


# --- GLEIF ADAPTER TESTS ---

@pytest.mark.asyncio
@respx.mock
async def test_gleif_adapter_valid_record(gleif_fixture: str):
    url = "https://api.gleif.org/api/v1/lei-records?filter[entity.registeredAs]=923%20609%20016"
    respx.get(url).respond(status_code=200, text=gleif_fixture)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = GleifAdapter()
        facts = await adapter.fetch("923609016", ctx)

        assert len(facts) == 5

        lei_fact = next(f for f in facts if f.field == "lei")
        assert lei_fact.value == "5493001KJTIIGC8Y1R12"
        assert lei_fact.source_url == url

        name_fact = next(f for f in facts if f.field == "legal_name")
        assert name_fact.value == "NORWEGIAN TECH LABS AS"

        status_fact = next(f for f in facts if f.field == "status")
        assert status_fact.value == "ISSUED"

        dp_fact = next(f for f in facts if f.field == "direct_parent")
        assert dp_fact.value == "549300PARENTDIRECT01"

        up_fact = next(f for f in facts if f.field == "ultimate_parent")
        assert up_fact.value == "549300PARENTULTIMA02"


@pytest.mark.asyncio
@respx.mock
async def test_gleif_adapter_empty_result():
    url = "https://api.gleif.org/api/v1/lei-records?filter[entity.registeredAs]=999%20999%20999"
    respx.get(url).respond(status_code=200, json={"data": []})

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = GleifAdapter()
        facts = await adapter.fetch("999999999", ctx)

        assert len(facts) == 0


@pytest.mark.asyncio
@respx.mock
async def test_gleif_adapter_rejects_unmatched_authority_or_country():
    url = "https://api.gleif.org/api/v1/lei-records?filter[entity.registeredAs]=923%20609%20016"
    respx.get(url).respond(
        status_code=200,
        json={
            "data": [
                {
                    "id": "549300WRONG123",
                    "attributes": {
                        "lei": "549300WRONG123",
                        "entity": {
                            "legalName": {"name": "FOREIGN ORG"},
                            "registeredAt": {"id": "RA000001"},
                            "legalAddress": {"country": "SE"},
                        },
                    },
                }
            ]
        },
    )

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = GleifAdapter()
        facts = await adapter.fetch("923609016", ctx)

        assert len(facts) == 0


@pytest.mark.asyncio
async def test_gleif_adapter_budget_exhausted():
    budget = BudgetTracker(max_requests=10)
    # Spend up to 75% limit for 'enrich' priority (7.5 -> 7 requests spent makes 8th call fail can_afford)
    budget.requests_spent = 8

    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = GleifAdapter()
        facts = await adapter.fetch("923609016", ctx)

        assert len(facts) == 0


# --- WIKIDATA ADAPTER TESTS ---

@pytest.mark.asyncio
@respx.mock
async def test_wikidata_adapter_valid_record(wikidata_fixture: str):
    url = "https://query.wikidata.org/sparql"
    respx.get(url).respond(status_code=200, text=wikidata_fixture)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = WikidataAdapter()
        facts = await adapter.fetch("923609016", ctx)

        assert len(facts) == 5

        qid_fact = next(f for f in facts if f.field == "qid")
        assert qid_fact.value == "Q11991850"
        assert qid_fact.match_basis == ["wikidata_property_p2333"]

        desc_fact = next(f for f in facts if f.field == "description")
        assert desc_fact.value == "Norwegian technology research company"

        inception_fact = next(f for f in facts if f.field == "inception")
        assert inception_fact.value == "2020-01-15"

        site_fact = next(f for f in facts if f.field == "official_website")
        assert site_fact.value == "https://norwaytechlabs.no"

        ind_fact = next(f for f in facts if f.field == "industry")
        assert ind_fact.value == "Software Engineering"


@pytest.mark.asyncio
@respx.mock
async def test_wikidata_adapter_empty_result():
    url = "https://query.wikidata.org/sparql"
    respx.get(url).respond(status_code=200, json={"results": {"bindings": []}})

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = WikidataAdapter()
        facts = await adapter.fetch("999999999", ctx)

        assert len(facts) == 0
        assert ctx.skipped_stages.get("wikidata") == "no record for this orgnr"


@pytest.mark.asyncio
async def test_wikidata_adapter_budget_exhausted():
    budget = BudgetTracker(max_requests=10)
    budget.requests_spent = 8

    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = WikidataAdapter()
        facts = await adapter.fetch("923609016", ctx)

        assert len(facts) == 0


@pytest.mark.asyncio
async def test_wikidata_property_label_mentions_norwegian_organisation_number():
    """Fetches the label of the property used from the Wikidata API and fails if it doesn't mention the Norwegian organisation number."""
    import httpx
    property_id = getattr(WikidataAdapter, "PROPERTY_ID", "P2333")
    url = f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={property_id}&props=labels&languages=en&format=json"
    headers = {"User-Agent": "NorwayProfiler/0.1.0 (contact: admin@norway-profiler.no)"}
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        
    assert resp.status_code == 200, f"Wikidata API returned HTTP {resp.status_code}"
    data = resp.json()
    entity = data.get("entities", {}).get(property_id, {})
    label = entity.get("labels", {}).get("en", {}).get("value", "")
    assert "norwegian organisation number" in label.lower() or "norwegian organization number" in label.lower(), (
        f"Property {property_id} label '{label}' does not mention Norwegian organisation number"
    )


# --- DOFFIN ADAPTER TESTS ---

@pytest.mark.asyncio
async def test_doffin_adapter_missing_api_key_makes_no_requests(monkeypatch):
    """When DOFFIN_API_KEY is not set, adapter returns [] without making any requests."""
    monkeypatch.delenv("DOFFIN_API_KEY", raising=False)
    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = DoffinAdapter()
        facts = await adapter.fetch("923609016", ctx)

        assert facts == []
        assert budget.requests_spent == 0


@pytest.mark.asyncio
@respx.mock
async def test_doffin_adapter_valid_records(doffin_fixture: str, monkeypatch):
    monkeypatch.setenv("DOFFIN_API_KEY", "test-subscription-key")
    url = "https://api.doffin.no/public/v2/notices"
    respx.get(url, params={"supplierOrgNum": "923609016"}).respond(status_code=200, text=doffin_fixture)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = DoffinAdapter()
        facts = await adapter.fetch("923609016", ctx)

        assert len(facts) == 1
        fact = facts[0]
        assert fact.field == "public_contracts"
        assert len(fact.value) == 5  # Capped at top 5 most recent

        # Verify sorted by date descending
        dates = [c["date"] for c in fact.value]
        assert dates == sorted(dates, reverse=True)
        assert fact.value[0]["title"] == "Cloud Infrastructure Modernization"
        assert fact.value[0]["buyer"] == "Oslo Kommune"


@pytest.mark.asyncio
@respx.mock
async def test_doffin_adapter_empty_result(monkeypatch):
    monkeypatch.setenv("DOFFIN_API_KEY", "test-subscription-key")
    url = "https://api.doffin.no/public/v2/notices"
    respx.get(url, params={"supplierOrgNum": "999999999"}).respond(status_code=200, json={"results": []})

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = DoffinAdapter()
        facts = await adapter.fetch("999999999", ctx)

        assert len(facts) == 0


@pytest.mark.asyncio
async def test_doffin_adapter_budget_exhausted(monkeypatch):
    monkeypatch.setenv("DOFFIN_API_KEY", "test-subscription-key")
    budget = BudgetTracker(max_requests=10)
    budget.requests_spent = 8

    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = DoffinAdapter()
        facts = await adapter.fetch("923609016", ctx)

        assert len(facts) == 0

