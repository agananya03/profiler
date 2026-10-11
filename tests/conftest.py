"""Pytest configuration and shared fixtures for offline testing (Rule 10)."""

import json
from pathlib import Path
import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    """Pytest fixture returning Path to tests/fixtures."""
    return FIXTURES_DIR


@pytest.fixture(autouse=True)
def set_test_env_vars(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemini-3.6-flash")


@pytest.fixture
def brreg_enhet_fixture_raw() -> str:
    """Load raw JSON text for Brreg Enhetsregisteret fixture."""
    return (FIXTURES_DIR / "brreg_enhet_923609016.json").read_text(encoding="utf-8")


@pytest.fixture
def brreg_enhet_fixture_json(brreg_enhet_fixture_raw: str) -> dict:
    """Load parsed JSON dictionary for Brreg Enhetsregisteret fixture."""
    return json.loads(brreg_enhet_fixture_raw)


@pytest.fixture
def brreg_regnskap_fixture_raw() -> str:
    """Load raw JSON text for Brreg Regnskapsregisteret fixture."""
    return (FIXTURES_DIR / "brreg_regnskap_923609016.json").read_text(encoding="utf-8")


@pytest.fixture
def brreg_regnskap_fixture_json(brreg_regnskap_fixture_raw: str) -> list:
    """Load parsed JSON list for Brreg Regnskapsregisteret fixture."""
    return json.loads(brreg_regnskap_fixture_raw)


@pytest.fixture
def sample_webpage_html() -> str:
    """Load raw HTML fixture."""
    return (FIXTURES_DIR / "sample_company_webpage.html").read_text(encoding="utf-8")
