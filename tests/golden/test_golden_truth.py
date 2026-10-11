"""Golden truth comparison test loading tests/golden/truth.csv."""

import csv
from pathlib import Path
import pytest
import respx

from profiler.adapters.brreg import BrregEnhetAdapter
from profiler.adapters.regnskap import BrregRegnskapAdapter
from profiler.orchestrator import Orchestrator

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

TRUTH_CSV_PATH = Path(__file__).parent / "truth.csv"


def load_truth_data() -> list[dict[str, str]]:
    """Load ground truth CSV file."""
    assert TRUTH_CSV_PATH.exists(), f"truth.csv not found at {TRUTH_CSV_PATH}"
    truth = []
    with TRUTH_CSV_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            truth.append(row)
    return truth


@pytest.mark.asyncio
@respx.mock
async def test_golden_truth_comparison(brreg_enhet_fixture_raw, brreg_regnskap_fixture_raw):
    """Compare agent profile output against hand-verified truth.csv ground truth."""
    truth_rows = load_truth_data()
    assert len(truth_rows) > 0

    # Mock 923609016 responses
    respx.get("https://data.brreg.no/enhetsregisteret/api/enheter/923609016").respond(
        status_code=200, text=brreg_enhet_fixture_raw
    )
    respx.get("https://data.brreg.no/regnskapsregisteret/regnskap/923609016").respond(
        status_code=200, text=brreg_regnskap_fixture_raw
    )

    # Mock 986161474 responses
    sample_986161474 = {
        "organisasjonsnummer": "986161474",
        "navn": "TORDENSKJOLD CONSULTING AS",
        "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
        "forretningsadresse": {"adresse": ["Storgata 1"], "postnummer": "0155", "poststed": "OSLO"},
        "antallAnsatte": 5,
        "naeringskode1": {"kode": "62.020", "beskrivelse": "IT-rådgivning"},
        "registreringsdatoEnhetsregisteret": "2010-05-15",
    }
    respx.get("https://data.brreg.no/enhetsregisteret/api/enheter/986161474").respond(
        status_code=200, json=sample_986161474
    )
    respx.get("https://data.brreg.no/regnskapsregisteret/regnskap/986161474").respond(
        status_code=404, json={"feilmelding": "Ikke funnet"}
    )

    async with Orchestrator(adapters=[BrregEnhetAdapter(), BrregRegnskapAdapter()]) as orchestrator:
        for row in truth_rows:
            orgnr = row["orgnr"]
            field = row["field"]
            expected_str = row["expected_value"]
            expected_source = row["source_url"]

            profile = await orchestrator.profile_company(orgnr)

            if field == "status":
                val = str(profile.status)
                assert val.lower() == expected_str.lower()
            else:
                matching_facts = [f for f in profile.facts if f.field == field]
                assert len(matching_facts) > 0, f"No fact found for orgnr={orgnr}, field={field}"

                fact = matching_facts[0]
                val = str(fact.value)
                assert val.lower() == expected_str.lower(), (
                    f"Mismatch for {orgnr} {field}: expected '{expected_str}', got '{val}'"
                )
                if expected_source:
                    assert fact.source_url == expected_source
