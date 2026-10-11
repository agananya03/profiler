"""Property and Fabrication tests (Rule 6, 7).

Property test:
- Asserts no published fact lacks source_url and retrieved_at.

Fabrication test:
- Runs in replay mode with no cached accounts and asserts every financial field is null with a reason.
"""

import pytest
import respx

from profiler.adapters.brreg import BrregEnhetAdapter
from profiler.adapters.regnskap import BrregRegnskapAdapter
from profiler.budget import Budget
from profiler.http import Http
from profiler.orchestrator import Orchestrator


@pytest.mark.asyncio
@respx.mock
async def test_property_no_published_fact_lacks_source_url_and_retrieved_at(
    brreg_enhet_fixture_raw, brreg_regnskap_fixture_raw
):
    """Property test: assert no published fact lacks source_url or retrieved_at."""
    respx.get("https://data.brreg.no/enhetsregisteret/api/enheter/923609016").respond(
        status_code=200, text=brreg_enhet_fixture_raw
    )
    respx.get("https://data.brreg.no/regnskapsregisteret/regnskap/923609016").respond(
        status_code=200, text=brreg_regnskap_fixture_raw
    )

    async with Orchestrator(adapters=[BrregEnhetAdapter(), BrregRegnskapAdapter()]) as orchestrator:
        profile = await orchestrator.profile_company("923609016")

        assert len(profile.facts) > 0
        for fact in profile.facts:
            assert fact.source_url is not None and str(fact.source_url).strip() != "", (
                f"Fact '{fact.field}' lacks source_url: {fact}"
            )
            assert fact.retrieved_at is not None and str(fact.retrieved_at).strip() != "", (
                f"Fact '{fact.field}' lacks retrieved_at: {fact}"
            )


@pytest.mark.asyncio
async def test_fabrication_replay_mode_no_cached_accounts():
    """Fabrication test: run in replay mode with no cached accounts and assert financial fields are null with a reason."""
    budget = Budget(max_requests=100)

    # Use replay mode with empty cache
    async with Http(budget=budget, cache_mode="replay") as http:
        async with Orchestrator(
            budget=budget,
            http_client=http,
            adapters=[BrregEnhetAdapter(), BrregRegnskapAdapter()],
        ) as orchestrator:
            profile = await orchestrator.profile_company("999999999")

            # Assert no financial facts were fabricated
            financial_facts = [
                f for f in profile.facts if f.field in ("financials", "revenue", "operating_profit", "total_assets")
            ]
            assert len(financial_facts) == 0, f"Fabricated financial facts found: {financial_facts}"

            # Assert nulls dictionary records reason for financial fields
            assert "financials" in profile.nulls or "financials" not in [f.field for f in profile.facts]
            reason = profile.nulls.get("financials", "")
            assert (
                "not_found" in reason
                or "cache_miss" in reason
                or "no_accounts" in reason
                or "no filed accounts" in reason
                or "budget_exceeded" in reason
                or "error" in reason
                or "fetch failed" in reason
            ), f"Unexpected null reason: {reason}"
