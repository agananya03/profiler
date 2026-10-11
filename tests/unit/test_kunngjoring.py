"""Unit tests for Kunngjøringer adapter deprecation/removal (Rule 9)."""

import pytest
import respx

from profiler.adapters.base import Ctx
from profiler.adapters.kunngjoring import BrregKunngjoringAdapter, _classify_announcement
from profiler.budget import BudgetTracker
from profiler.http import HttpClient
from profiler.orchestrator import Orchestrator


@pytest.mark.asyncio
@respx.mock
async def test_kunngjoring_adapter_makes_no_network_calls():
    """Verify that BrregKunngjoringAdapter does not call any Kunngjøringer endpoints and returns []."""
    # Any HTTP call to brreg kunngjoringer should fail if attempted
    respx.get("https://data.brreg.no/kunngjoringer/2/api/kunngjoringer?orgnr=923609016").respond(status_code=500)
    respx.get("https://data.brreg.no/enhetsregisteret/api/enheter/923609016/kunngjoringer").respond(status_code=500)

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        adapter = BrregKunngjoringAdapter()
        facts = await adapter.fetch("923609016", ctx)

        assert facts == []
        assert budget.requests_spent == 0


def test_classify_announcement():
    """Test classification helper function for announcements."""
    assert _classify_announcement("Åpning av konkurs") == "bankruptcy"
    assert _classify_announcement("Beslutning om tvangsavvikling") == "bankruptcy"
    assert _classify_announcement("Oppløsning av selskap") == "liquidation"
    assert _classify_announcement("Fusjon med Equinor") == "merger"
    assert _classify_announcement("Fisjon av virksomhet") == "demerger"
    assert _classify_announcement("Endring av foretaksnavn") == "name_change"
    assert _classify_announcement("Forhøyelse av aksjekapital") == "capital_change"
    assert _classify_announcement("Årsregnskap mottatt") == "other"


@pytest.mark.asyncio
async def test_orchestrator_deregistered_kunngjoring_and_skipped_reason():
    """Test that Orchestrator has deregistered Kunngjøringer and records the explicit skipped reason."""
    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        orch = Orchestrator(http_client=http, budget=budget)

        # Assert BrregKunngjoringAdapter is not in stage_2_details
        stage_names = [a.name for a in orch.stage_2_details]
        assert "kunngjoring" not in stage_names

        ctx = Ctx(http=http, budget=budget)
        profile = orch.compile_profile("923609016", [], ctx.errors, ctx)

        assert "kunngjoring" in profile.run_meta["skipped_stages"]
        assert profile.run_meta["skipped_stages"]["kunngjoring"] == (
            "no documented public API; lifecycle status taken from Enhetsregisteret fields"
        )
