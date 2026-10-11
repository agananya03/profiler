"""Unit tests for src/profiler/llm.py."""

import json
from pathlib import Path
import pytest
import respx

from profiler.budget import BudgetTracker
from profiler.llm import (
    ALLOWED_LLM_FIELDS,
    FakeLLMClient,
    LLMClient,
    LLMError,
    PROMPT_VERSION,
    get_llm_model,
    validate_llm_fact,
    verify_quote,
)


def test_get_llm_model_unset_raises_error(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(ValueError, match="LLM_MODEL environment variable must be set"):
        get_llm_model()


def test_get_llm_model_custom(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemini-3.6-flash")
    assert get_llm_model() == "gemini-3.6-flash"


def test_verify_quote_valid():
    page_text = "Norwegian Tech Labs AS provides enterprise cloud software solutions across Scandinavia and Europe."
    quote = "Norwegian Tech Labs AS provides enterprise cloud software solutions across Scandinavia"
    value = "enterprise cloud software solutions"

    assert verify_quote(quote, page_text, value) is True


def test_verify_quote_fabricated_fails():
    page_text = "Norwegian Tech Labs AS provides enterprise cloud software solutions across Scandinavia."
    quote = "This company is the market leader in quantum artificial intelligence solutions."  # Fabricated
    value = "quantum artificial intelligence"

    assert verify_quote(quote, page_text, value) is False


def test_verify_quote_too_short_fails():
    page_text = "Norwegian Tech Labs AS provides enterprise cloud software solutions across Scandinavia."
    quote = "Short quote here"  # 16 chars < 40
    value = "cloud software"

    assert verify_quote(quote, page_text, value) is False


def test_verify_quote_too_long_fails():
    page_text = "A" * 500
    quote = "A" * 305  # > 300 chars
    value = "AAAA"

    assert verify_quote(quote, page_text, value) is False


def test_verify_quote_value_tokens_missing_fails():
    page_text = "Norwegian Tech Labs AS provides enterprise cloud software solutions across Scandinavia."
    quote = "Norwegian Tech Labs AS provides enterprise cloud software solutions across Scandinavia"
    value = "unrelated cybersecurity firewall service"  # Value tokens not in quote

    assert verify_quote(quote, page_text, value) is False


@pytest.mark.asyncio
async def test_fake_llm_client_fabricated_quote_yields_zero_facts(tmp_path: Path):
    page_text = "Norwegian Tech Labs AS develops web software solutions for Norwegian clients."
    fake_items = [
        {
            "field": "description",
            "value": "Market leading AI platform",
            "quote": "This quote does not exist on the company page text at all.",
        }
    ]

    budget = BudgetTracker()
    client = FakeLLMClient(fake_items=fake_items, cache_dir=str(tmp_path / "llm_cache"))

    facts = await client.extract_facts(
        page_text=page_text,
        source_url="https://norwaytechlabs.no",
        orgnr="923609016",
        match_basis=["domain_match"],
        budget=budget,
    )

    assert len(facts) == 0
    assert client.discarded_count == 1


@pytest.mark.asyncio
async def test_fake_llm_client_valid_quote_creates_fact(tmp_path: Path):
    page_text = "Norwegian Tech Labs AS provides enterprise cloud software solutions across Scandinavia and Europe."
    quote = "Norwegian Tech Labs AS provides enterprise cloud software solutions across Scandinavia"
    value = "enterprise cloud software solutions"
    fake_items = [
        {
            "field": "description",
            "value": value,
            "quote": quote,
        }
    ]

    budget = BudgetTracker()
    client = FakeLLMClient(fake_items=fake_items, cache_dir=str(tmp_path / "llm_cache"))

    facts = await client.extract_facts(
        page_text=page_text,
        source_url="https://norwaytechlabs.no",
        orgnr="923609016",
        match_basis=["domain_match"],
        budget=budget,
    )

    assert len(facts) == 1
    assert facts[0].field == "description"
    assert facts[0].value == value
    assert facts[0].evidence_quote == quote
    assert facts[0].method == "llm_verified"
    assert budget.usd_spent > 0


@pytest.mark.asyncio
async def test_fake_llm_client_forbidden_financial_field_rejected(tmp_path: Path):
    page_text = "Revenue in 2023 was 15000000 NOK. Norwegian Tech Labs AS provides enterprise cloud software solutions."
    quote = "Revenue in 2023 was 15000000 NOK. Norwegian Tech Labs AS provides enterprise cloud software solutions."
    fake_items = [
        {
            "field": "revenue",
            "value": 15000000.0,
            "quote": quote,
        }
    ]

    budget = BudgetTracker()
    client = FakeLLMClient(fake_items=fake_items, cache_dir=str(tmp_path / "llm_cache"))

    facts = await client.extract_facts(
        page_text=page_text,
        source_url="https://norwaytechlabs.no",
        orgnr="923609016",
        match_basis=["domain_match"],
        budget=budget,
    )

    assert len(facts) == 0
    assert client.discarded_count == 1


@pytest.mark.asyncio
async def test_llm_caching(tmp_path: Path):
    page_text = "Norwegian Tech Labs AS provides enterprise cloud software solutions across Scandinavia and Europe."
    quote = "Norwegian Tech Labs AS provides enterprise cloud software solutions across Scandinavia"
    value = "enterprise cloud software solutions"
    fake_items = [
        {
            "field": "description",
            "value": value,
            "quote": quote,
        }
    ]

    budget = BudgetTracker()
    client = FakeLLMClient(fake_items=fake_items, cache_dir=str(tmp_path / "llm_cache"))

    # First call - populates cache
    facts1 = await client.extract_facts(
        page_text=page_text,
        source_url="https://norwaytechlabs.no",
        orgnr="923609016",
        match_basis=["domain_match"],
        budget=budget,
    )
    initial_usd = budget.usd_spent

    # Second call - should hit cache and not charge extra budget
    facts2 = await client.extract_facts(
        page_text=page_text,
        source_url="https://norwaytechlabs.no",
        orgnr="923609016",
        match_basis=["domain_match"],
        budget=budget,
    )

    assert len(facts1) == 1
    assert len(facts2) == 1
    assert budget.usd_spent == initial_usd


@pytest.mark.asyncio
async def test_valid_quote_from_page_text_yields_fact_with_crawled_page_source_url(tmp_path: Path):
    page_text = "Acme AS er et norsk selskap som leverer programvare til banker og finansinstitusjoner."
    crawled_url = "https://acme.no/om-oss"
    match_basis = ["orgnr_exact", "domain_from_brreg"]
    valid_item = {
        "field": "products_services",
        "value": "programvare til banker",
        "quote": "Acme AS er et norsk selskap som leverer programvare til banker og finansinstitusjoner.",
    }
    budget = BudgetTracker()
    client = FakeLLMClient(fake_items=[valid_item], cache_dir=str(tmp_path / "llm_cache"))
    facts = await client.extract_facts(
        page_text=page_text,
        source_url=crawled_url,
        orgnr="923609016",
        match_basis=match_basis,
        budget=budget,
    )
    assert len(facts) == 1
    assert facts[0].field == "products_services"
    assert facts[0].source_url == crawled_url
    assert facts[0].match_basis == ["orgnr_exact", "domain_from_brreg"]


@pytest.mark.asyncio
async def test_fabricated_quote_yields_zero_facts(tmp_path: Path):
    page_text = "Acme AS leverer programvare for regnskap og lønn."
    fabricated_item = {
        "field": "description",
        "value": "Verdensledende innen romfartsteknologi",
        "quote": "Acme AS er verdensledende innen romfartsteknologi og satellittkontroll.",
    }
    budget = BudgetTracker()
    client = FakeLLMClient(fake_items=[fabricated_item], cache_dir=str(tmp_path / "llm_cache"))
    facts = await client.extract_facts(
        page_text=page_text,
        source_url="https://acme.no",
        orgnr="923609016",
        match_basis=["orgnr_exact", "domain_from_brreg"],
        budget=budget,
    )
    assert len(facts) == 0
    assert client.discarded_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 400])
@respx.mock
async def test_http_error_raises_llmerror_and_not_cached(tmp_path: Path, status_code: int):
    model = "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    respx.post(url).respond(status_code=status_code, text=f"Error {status_code}: Request Failed")

    cache_dir = tmp_path / "llm_cache"
    client = LLMClient(api_key="test-api-key", model_name=model, cache_dir=str(cache_dir))
    budget = BudgetTracker()

    with pytest.raises(LLMError, match=f"HTTP {status_code}"):
        await client.extract_facts(
            page_text="Acme AS leverer programvare.",
            source_url="https://acme.no",
            orgnr="923609016",
            match_basis=["domain_match"],
            budget=budget,
        )

    # Assert not cached
    cached_files = list(cache_dir.glob("*.json"))
    assert len(cached_files) == 0


@pytest.mark.asyncio
@respx.mock
async def test_stale_empty_cache_entries_from_older_prompt_version_are_ignored(tmp_path: Path):
    model = "gemini-2.5-flash"
    cache_dir = tmp_path / "llm_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    page_text = "Acme AS leverer skytjenester og moderne programvare til offentlig sektor."

    # Pre-populate stale cache entry with older prompt_version
    client = LLMClient(api_key="test-api-key", model_name=model, cache_dir=str(cache_dir))
    cache_key = client._compute_cache_key(page_text[:24_000])
    stale_cache_file = cache_dir / f"{cache_key}.json"

    stale_payload = {
        "cache_key": cache_key,
        "prompt_version": "v0.9-stale",
        "model": model,
        "extracted_items": [],
    }
    stale_cache_file.write_text(json.dumps(stale_payload), encoding="utf-8")

    # Mock API call returning fresh valid item
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    valid_response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps([
                                {
                                    "field": "products_services",
                                    "value": "skytjenester og moderne programvare",
                                    "quote": "Acme AS leverer skytjenester og moderne programvare til offentlig sektor.",
                                }
                            ])
                        }
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 120,
            "candidatesTokenCount": 60,
            "thoughtsTokenCount": 0,
        },
    }
    respx.post(url).respond(status_code=200, json=valid_response)

    budget = BudgetTracker()
    facts = await client.extract_facts(
        page_text=page_text,
        source_url="https://acme.no",
        orgnr="923609016",
        match_basis=["orgnr_exact", "domain_from_brreg"],
        budget=budget,
    )

    assert len(facts) == 1
    assert facts[0].field == "products_services"

    # Verify the updated cache file has current PROMPT_VERSION
    updated_data = json.loads(stale_cache_file.read_text(encoding="utf-8"))
    assert updated_data["prompt_version"] == PROMPT_VERSION
    assert len(updated_data["extracted_items"]) == 1
