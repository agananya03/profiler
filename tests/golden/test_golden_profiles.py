"""Golden integration tests comparing full profile outputs against expected schema."""

import pytest
import respx

from profiler.adapters.brreg import BrregEnhetAdapter
from profiler.adapters.regnskap import BrregRegnskapAdapter
from profiler.orchestrator import Orchestrator
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.mark.asyncio
@respx.mock
async def test_golden_company_profile(brreg_enhet_fixture_raw, brreg_regnskap_fixture_raw):
    respx.get("https://data.brreg.no/enhetsregisteret/api/enheter/923609016").respond(
        status_code=200, text=brreg_enhet_fixture_raw
    )
    respx.get("https://data.brreg.no/regnskapsregisteret/regnskap/923609016").respond(
        status_code=200, text=brreg_regnskap_fixture_raw
    )

    async with Orchestrator(adapters=[BrregEnhetAdapter(), BrregRegnskapAdapter()]) as orchestrator:
        profile = await orchestrator.profile_company("923609016")

        assert profile.orgnr == "923609016"
        assert profile.status == "active"
        assert len(profile.facts) > 0

        name_fact = next(f for f in profile.facts if f.field == "legal_name")
        assert name_fact.value == "EQUINOR ASA"

        fin_fact = next(f for f in profile.facts if f.field == "financials")
        import json
        raw = json.loads((FIXTURES_DIR / "regnskap_923609016.json").read_text(encoding="utf-8"))
        latest_end = max(x["regnskapsperiode"]["tilDato"] for x in raw)
        konsern = next(
            x for x in raw
            if x["regnskapstype"] == "KONSERN"
            and x["regnskapsperiode"]["tilDato"] == latest_end
        )
        expected_revenue = konsern["resultatregnskapResultat"]["driftsresultat"]["driftsinntekter"]["sumDriftsinntekter"]

        assert fin_fact.value["revenue"] == expected_revenue
        assert fin_fact.value["fiscal_year_end"] == latest_end
        assert fin_fact.value["currency"] == konsern["valuta"]
        assert fin_fact.value["is_consolidated"] is True
