"""Unit tests for BrregRegnskapAdapter."""

from pathlib import Path
import pytest
import respx

from profiler.adapters.base import Ctx
from profiler.adapters.regnskap import BrregRegnskapAdapter
from profiler.budget import BudgetTracker
from profiler.http import HttpClient
from profiler.orchestrator import Orchestrator

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def regnskap_923609016_fixture() -> str:
    return (FIXTURES_DIR / "regnskap_923609016.json").read_text(encoding="utf-8")


@pytest.fixture
def regnskap_500_bank_fixture() -> str:
    return (FIXTURES_DIR / "brreg_regnskap_984851006.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
@respx.mock
async def test_regnskap_adapter_923609016_fixture(regnskap_923609016_fixture: str):
    """Test fetching and parsing real fixture regnskap_923609016.json."""
    url = "https://data.brreg.no/regnskapsregisteret/regnskap/923609016"
    respx.get(url).respond(status_code=200, text=regnskap_923609016_fixture)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = BrregRegnskapAdapter()
        facts = await adapter.fetch("923609016", ctx)

        assert len(facts) > 0
        latest_fact = facts[0]
        fin = latest_fact.value

        # Requirement 2: Latest fiscal year (2025) selected first
        assert fin["year"] == 2025
        assert fin["fiscal_year_start"] == "2025-01-01"
        assert fin["fiscal_year_end"] == "2025-12-31"
        assert latest_fact.period == "2025"

        # Requirement 3: Currency and consolidation from response itself
        assert fin["currency"] == "USD"
        assert fin["is_consolidated"] is True
        assert fin["currency_reason"] is None
        assert fin["consolidation_reason"] is None

        # Requirement 4 & 5: Canonical metrics & missing_fields list
        assert fin["total_assets"] == 131727000000.0
        assert fin["equity"] == 40497000000.0
        assert fin["total_debt"] == 91230000000.0
        assert fin["revenue"] == 106462000000.0
        assert fin["operating_result"] == 25353000000.0
        assert fin["net_result"] == 5058000000.0
        assert fin["missing_fields"] == []


@pytest.mark.asyncio
@respx.mock
async def test_regnskap_adapter_404():
    """Test HTTP 404 produces 'no filed accounts found' in nulls['financials']."""
    url = "https://data.brreg.no/regnskapsregisteret/regnskap/999999999"
    respx.get(url).respond(status_code=404)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = BrregRegnskapAdapter()
        facts = await adapter.fetch("999999999", ctx)
        assert len(facts) == 0

        orch = Orchestrator()
        profile = orch.compile_profile("999999999", facts, ctx.errors, ctx)
        assert profile.nulls["financials"] == "no filed accounts found"


@pytest.mark.asyncio
@respx.mock
async def test_regnskap_adapter_empty_list():
    """Test empty list response [] produces 'no filed accounts found' in nulls['financials']."""
    url = "https://data.brreg.no/regnskapsregisteret/regnskap/999999998"
    respx.get(url).respond(status_code=200, json=[])

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = BrregRegnskapAdapter()
        facts = await adapter.fetch("999999998", ctx)
        assert len(facts) == 0

        orch = Orchestrator()
        profile = orch.compile_profile("999999998", facts, ctx.errors, ctx)
        assert profile.nulls["financials"] == "no filed accounts found"


@pytest.mark.asyncio
@respx.mock
async def test_regnskap_adapter_500_bank(regnskap_500_bank_fixture: str):
    """Test HTTP 500 BANK format error produces 'unsupported accounts format (BANK) in Regnskapsregisteret'."""
    url = "https://data.brreg.no/regnskapsregisteret/regnskap/984851006"
    respx.get(url).respond(status_code=500, text=regnskap_500_bank_fixture)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = BrregRegnskapAdapter()
        facts = await adapter.fetch("984851006", ctx)

        assert len(facts) == 0
        assert len(ctx.errors) == 1

        orch = Orchestrator()
        profile = orch.compile_profile("984851006", facts, ctx.errors, ctx)
        assert profile.nulls["financials"] == "unsupported accounts format (BANK) in Regnskapsregisteret"


@pytest.mark.asyncio
@respx.mock
async def test_regnskap_adapter_503_error():
    """Test general HTTP failure produces 'fetch failed: ...' in nulls['financials']."""
    url = "https://data.brreg.no/regnskapsregisteret/regnskap/974760846"
    respx.get(url).respond(status_code=503, text="Service Unavailable")

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = BrregRegnskapAdapter()
        facts = await adapter.fetch("974760846", ctx)

        assert len(facts) == 0

        orch = Orchestrator()
        profile = orch.compile_profile("974760846", facts, ctx.errors, ctx)
        assert "financials" in profile.nulls
        assert profile.nulls["financials"].startswith("fetch failed:")

