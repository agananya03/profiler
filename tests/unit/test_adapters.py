"""Offline unit tests for adapters using respx and recorded fixtures."""

from pathlib import Path
import pytest
import respx

from profiler.adapters.base import Ctx
from profiler.adapters.brreg import BrregEnhetAdapter
from profiler.adapters.regnskap import BrregRegnskapAdapter
from profiler.adapters.website import WebsiteAdapter
from profiler.budget import BudgetTracker
from profiler.http import HttpClient

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def brreg_enhet_fixture() -> str:
    return (FIXTURES_DIR / "brreg_enhet_923609016.json").read_text(encoding="utf-8")


@pytest.fixture
def brreg_active_as_fixture() -> str:
    return (FIXTURES_DIR / "brreg_active_as.json").read_text(encoding="utf-8")


@pytest.fixture
def brreg_active_ans_fixture() -> str:
    return (FIXTURES_DIR / "brreg_active_ans.json").read_text(encoding="utf-8")


@pytest.fixture
def brreg_branch_fixture() -> str:
    return (FIXTURES_DIR / "brreg_branch.json").read_text(encoding="utf-8")


@pytest.fixture
def brreg_bankrupt_fixture() -> str:
    return (FIXTURES_DIR / "brreg_bankrupt.json").read_text(encoding="utf-8")


@pytest.fixture
def brreg_regnskap_fixture() -> str:
    return (FIXTURES_DIR / "regnskap_923609016.json").read_text(encoding="utf-8")

@pytest.mark.asyncio
@respx.mock
async def test_brreg_enhet_adapter(brreg_enhet_fixture: str):
    respx.get("https://data.brreg.no/enhetsregisteret/api/enheter/923609016").respond(
        status_code=200, text=brreg_enhet_fixture
    )

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = BrregEnhetAdapter()
        facts = await adapter.fetch("923609016", ctx)

        assert len(facts) > 0
        name_fact = next(f for f in facts if f.field == "legal_name")
        assert name_fact.value == "EQUINOR ASA"
        assert name_fact.source_url == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"
        assert name_fact.confidence == 0.99
        assert name_fact.match_basis == ["orgnr_exact"]


@pytest.mark.asyncio
@respx.mock
async def test_brreg_active_as_fixture(brreg_active_as_fixture: str):
    url = "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"
    respx.get(url).respond(status_code=200, text=brreg_active_as_fixture)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = BrregEnhetAdapter()
        facts = await adapter.fetch("923609016", ctx)

        legal_name = next(f for f in facts if f.field == "legal_name")
        assert legal_name.value == "EQUINOR ASA"
        assert legal_name.source_url == url
        assert legal_name.confidence == 0.99
        assert legal_name.match_basis == ["orgnr_exact"]

        legal_form = next(f for f in facts if f.field == "legal_form")
        assert legal_form.value == "ASA"

        emp_count = next(f for f in facts if f.field == "employee_count")
        assert emp_count.value == 21272

        nace_fact = next(f for f in facts if f.field == "nace_codes")
        assert len(nace_fact.value) >= 1
        assert nace_fact.value[0]["code"] == "06.100"
        assert nace_fact.value[0]["description"] == "Utvinning av råolje"

        share_cap = next(f for f in facts if f.field == "share_capital")
        assert share_cap.value == 5976872600.00


@pytest.mark.asyncio
@respx.mock
async def test_brreg_website_markdown_link_normalization():
    url = "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"
    data = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "hjemmeside": "www.equinor.com",
    }
    respx.get(url).respond(status_code=200, json=data)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = BrregEnhetAdapter()
        facts = await adapter.fetch("923609016", ctx)

        website_fact = next(f for f in facts if f.field == "website")
        assert website_fact.value == "https://www.equinor.com"


@pytest.mark.asyncio
@respx.mock
async def test_brreg_active_ans_fixture(brreg_active_ans_fixture: str):
    url = "https://data.brreg.no/enhetsregisteret/api/enheter/991272658"
    respx.get(url).respond(status_code=200, text=brreg_active_ans_fixture)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = BrregEnhetAdapter()
        facts = await adapter.fetch("991272658", ctx)

        legal_name = next(f for f in facts if f.field == "legal_name")
        assert legal_name.value == "10. APRIL ANS"

        legal_form = next(f for f in facts if f.field == "legal_form")
        assert legal_form.value == "ANS"

        vat_reg = next(f for f in facts if f.field == "vat_registered")
        assert vat_reg.value is True

        bus_reg = next(f for f in facts if f.field == "in_business_register")
        assert bus_reg.value is True


@pytest.mark.asyncio
@respx.mock
async def test_brreg_branch_fixture(brreg_branch_fixture: str):
    enheter_url = "https://data.brreg.no/enhetsregisteret/api/enheter/933489272"
    underenheter_url = "https://data.brreg.no/enhetsregisteret/api/underenheter/933489272"

    respx.get(enheter_url).respond(status_code=404)
    respx.get(underenheter_url).respond(status_code=200, text=brreg_branch_fixture)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = BrregEnhetAdapter()
        facts = await adapter.fetch("933489272", ctx)

        legal_form = next(f for f in facts if f.field == "legal_form")
        assert legal_form.value == "branch"
        assert legal_form.source_url == underenheter_url

        parent_org = next(f for f in facts if f.field == "parent_orgnr")
        assert parent_org.value == "933365573"


@pytest.mark.asyncio
@respx.mock
async def test_brreg_bankrupt_fixture(brreg_bankrupt_fixture: str):
    url = "https://data.brreg.no/enhetsregisteret/api/enheter/935567645"
    respx.get(url).respond(status_code=200, text=brreg_bankrupt_fixture)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = BrregEnhetAdapter()
        facts = await adapter.fetch("935567645", ctx)

        bankruptcy = next(f for f in facts if f.field == "is_bankrupt")
        assert bankruptcy.value is True

        legal_name = next(f for f in facts if f.field == "legal_name")
        assert legal_name.value == "1SB JERNBANEKAFEEN AS"





@pytest.mark.asyncio
@respx.mock
async def test_website_adapter_rejects_page_without_orgnr():
    url = "https://example.com/no-orgnr"
    respx.get(url).respond(status_code=200, text="<html><body>No orgnr here</body></html>")

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = WebsiteAdapter(target_url=url)
        facts = await adapter.fetch("923609016", ctx)

        # Rule 5: Discards page without orgnr proof
        assert len(facts) == 0


@pytest.mark.asyncio
@respx.mock
async def test_brreg_sub_units_fixture():
    enheter_url = "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"
    underenheter_url = "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet=923609016"

    enhet_text = (FIXTURES_DIR / "brreg_active_as.json").read_text(encoding="utf-8")
    sub_units_text = (FIXTURES_DIR / "brreg_sub_units_923609016.json").read_text(encoding="utf-8")

    respx.get(enheter_url).respond(status_code=200, text=enhet_text)
    respx.get(underenheter_url).respond(status_code=200, text=sub_units_text)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = BrregEnhetAdapter()
        facts = await adapter.fetch("923609016", ctx)

        sub_units_fact = next(f for f in facts if f.field == "sub_units")
        val = sub_units_fact.value
        assert val["total_count"] == 35
        assert val["truncated"] is True
        assert len(val["branches"]) <= 25
        assert val["branches"][0]["orgnr"] == "973861883"
