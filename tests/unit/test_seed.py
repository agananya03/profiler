"""Unit tests for src/profiler/seed.py."""

import json
from pathlib import Path
import pytest

from profiler.seed import (
    SeedGenerator,
    generate_valid_mod11_orgnr,
    get_employee_band,
    get_nace_section,
    run_seed_generator,
)
from profiler.validate import valid_orgnr


def test_mod11_generator():
    """Verify that generate_valid_mod11_orgnr creates valid Modulo-11 org numbers."""
    for i in range(1, 20):
        orgnr = generate_valid_mod11_orgnr(i)
        assert len(orgnr) == 9
        assert valid_orgnr(orgnr)


def test_employee_band_mapping():
    """Test get_employee_band mappings."""
    assert get_employee_band(0) == "0"
    assert get_employee_band(None) == "0"
    assert get_employee_band(3) == "1-4"
    assert get_employee_band(12) == "5-19"
    assert get_employee_band(50) == "20-99"
    assert get_employee_band(250) == "100+"


def test_nace_section_mapping():
    """Test get_nace_section mappings."""
    assert get_nace_section("62.012") == "J - Information and Communication"
    assert get_nace_section("41.200") == "F - Construction"
    assert get_nace_section("01.110") == "A - Agriculture, Forestry, Fishing"
    assert get_nace_section(None) == "M - Professional, Scientific, Technical"


@pytest.mark.asyncio
async def test_run_seed_generator(tmp_path: Path):
    """Test seed dataset generation and output file creation."""
    out_txt = tmp_path / "seed.txt"
    summary_json = tmp_path / "seed_summary.json"

    orgnrs, summary = await run_seed_generator(
        count=50,
        out_path=out_txt,
        summary_path=summary_json,
        hard_cases=True,
    )

    assert len(orgnrs) == 50
    assert out_txt.exists()
    assert summary_json.exists()

    # All org numbers must be valid
    for org in orgnrs:
        assert valid_orgnr(org)

    # Check summary structure
    data = json.loads(summary_json.read_text(encoding="utf-8"))
    assert data["total_count"] == 50
    assert "legal_forms" in data
    assert "counties" in data
    assert "employee_bands" in data
    assert "nace_sections" in data
    assert data["hard_cases_count"] > 0
