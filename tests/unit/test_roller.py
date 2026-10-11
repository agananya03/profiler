"""Unit tests for BrregRollerAdapter."""

from pathlib import Path
import pytest
import respx

from profiler.adapters.base import Ctx
from profiler.adapters.roller import BrregRollerAdapter
from profiler.budget import BudgetTracker
from profiler.http import HttpClient

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def roller_fixture() -> str:
    return (FIXTURES_DIR / "brreg_roller_923609016.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
@respx.mock
async def test_brreg_roller_adapter_success(roller_fixture: str):
    url = "https://data.brreg.no/enhetsregisteret/api/enheter/923609016/roller"
    respx.get(url).respond(status_code=200, text=roller_fixture)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = BrregRollerAdapter()
        facts = await adapter.fetch("923609016", ctx)

        assert len(facts) >= 1
        roles_fact = next(f for f in facts if f.field == "roles")
        assert len(roles_fact.value) == 3

        ceo_role = next(r for r in roles_fact.value if r["role_code"] == "DAGL")
        assert ceo_role["name"] == "Anders Opedal"
        assert ceo_role["role_category"] == "CEO"
        assert ceo_role["resigned"] is False

        ceo_fact = next(f for f in facts if f.field == "ceo")
        assert ceo_fact.value == "Anders Opedal"

        chair_fact = next(f for f in facts if f.field == "chair")
        assert chair_fact.value == "Jon Erik"

        auditor_fact = next(f for f in facts if f.field == "auditor")
        assert "PRICEWATERHOUSECOOPERS" in auditor_fact.value


@pytest.mark.asyncio
@respx.mock
async def test_brreg_roller_adapter_404():
    url = "https://data.brreg.no/enhetsregisteret/api/enheter/999999999/roller"
    respx.get(url).respond(status_code=404)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = BrregRollerAdapter()
        facts = await adapter.fetch("999999999", ctx)
        assert len(facts) == 0
