"""Unit tests for models and budget tracker."""

from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from profiler.budget import BudgetExceededError, BudgetTracker
from profiler.llm import validate_llm_fact
from profiler.models import Fact


def test_fact_provenance_validation():
    now = datetime.now(timezone.utc)
    fact = Fact(
        orgnr="923609016",
        field="name",
        value="NORWEGIAN TECH LABS AS",
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        retrieved_at=now,
        method="api",
        match_basis=["orgnr_exact_api_endpoint"],
    )
    assert fact.source_url != ""


def test_rule_5_name_only_rejected():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError, match="Matching by name alone is strictly forbidden"):
        Fact(
            orgnr="923609016",
            field="name",
            value="TEST",
            source_name="Web Scraper",
            source_url="https://example.com",
            retrieved_at=now,
            method="parsed",
            match_basis=["name_only"],
        )


def test_rule_4_llm_verbatim_quote_validation():
    page_text = "Norwegian Tech Labs AS provides enterprise cloud software."
    quote = "Norwegian Tech Labs AS provides enterprise cloud software."
    value = "enterprise cloud software"

    assert validate_llm_fact("description", value, quote, page_text) is True

    # Missing verbatim quote
    assert validate_llm_fact("description", value, "Not in page", page_text) is False


def test_rule_3_llm_financial_values_forbidden():
    page_text = "Revenue in 2023 was 15000000 NOK."
    quote = "Revenue in 2023 was 15000000 NOK."
    value = 15000000.0

    assert validate_llm_fact("revenue", value, quote, page_text) is False


def test_budget_tracker():
    budget = BudgetTracker(max_requests=2)
    budget.record_request()
    budget.record_request()
    with pytest.raises(BudgetExceededError):
        budget.record_request()
