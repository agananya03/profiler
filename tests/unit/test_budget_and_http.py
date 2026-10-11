"""Tests for budget priorities, USD limits, HTTP client caching, retries, and respx integration."""

import os
from pathlib import Path
import pytest
import respx
from httpx import Response

from profiler.budget import Budget, BudgetExceeded
from profiler.http import CacheMissError, Http


def test_budget_priority_ceilings():
    # max_requests = 10
    # core: 90% -> 9 max
    # enrich: 75% -> 7 max
    # web: 60% -> 6 max
    budget = Budget(max_requests=10)

    # Web priority cap (6 requests)
    for _ in range(6):
        assert budget.can_afford(1, priority="web") is True
        budget.spend(1, priority="web")

    # 7th web request should be rejected
    assert budget.can_afford(1, priority="web") is False
    with pytest.raises(BudgetExceeded, match="priority 'web'"):
        budget.spend(1, priority="web")

    # Enrich priority can continue up to 7
    assert budget.can_afford(1, priority="enrich") is True
    budget.spend(1, priority="enrich")
    assert budget.can_afford(1, priority="enrich") is False

    # Core priority can continue up to 9
    assert budget.can_afford(1, priority="core") is True
    budget.spend(1, priority="core")
    budget.spend(1, priority="core")
    assert budget.can_afford(1, priority="core") is False


def test_budget_usd_spent_cap():
    budget = Budget(max_usd=5.0)
    budget.spend_usd(2.5)
    assert budget.usd_spent == 2.5

    with pytest.raises(BudgetExceeded, match="USD budget limit exceeded"):
        budget.spend_usd(3.0)


@pytest.mark.asyncio
@respx.mock
async def test_http_budget_stops_requests():
    budget = Budget(max_requests=2)
    respx.get("https://api.example.com/item").respond(status_code=200, json={"ok": True})

    async with Http(budget=budget, cache_mode="live") as http:
        resp1 = await http.get("https://api.example.com/item")
        assert resp1.status_code == 200
        assert resp1.content_hash is not None
        assert resp1.retrieved_at is not None

        resp2 = await http.get("https://api.example.com/item")
        assert resp2.status_code == 200

        # 3rd request breaches budget
        with pytest.raises(BudgetExceeded):
            await http.get("https://api.example.com/item")


@pytest.mark.asyncio
@respx.mock
async def test_http_retries_count_in_budget():
    budget = Budget(max_requests=5)
    # 500 status code triggers tenacity retry
    route = respx.get("https://api.example.com/flaky").respond(status_code=500)

    async with Http(budget=budget, cache_mode="live") as http:
        with pytest.raises(Exception):
            await http.get("https://api.example.com/flaky")

        # 3 attempts (1 initial + 2 retries) were executed and counted in budget
        assert route.call_count == 3
        assert budget.requests_spent == 3


@pytest.mark.asyncio
@respx.mock
async def test_http_record_and_replay_mode(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    url = "https://api.example.com/data"
    respx.get(url).respond(status_code=200, json={"message": "recorded"})

    # Phase 1: Record mode
    budget_rec = Budget(max_requests=10)
    async with Http(budget=budget_rec, cache_mode="record", cache_dir=cache_dir) as http_rec:
        resp = await http_rec.get(url)
        assert resp.json() == {"message": "recorded"}
        assert budget_rec.requests_spent == 1

    # Phase 2: Replay mode (Offline - no network requests)
    budget_rep = Budget(max_requests=10)
    async with Http(budget=budget_rep, cache_mode="replay", cache_dir=cache_dir) as http_rep:
        # Cached hit works without network or spending budget
        cached_resp = await http_rep.get(url)
        assert cached_resp.json() == {"message": "recorded"}
        assert budget_rep.requests_spent == 0

        # Uncached request in replay mode raises CacheMissError
        with pytest.raises(CacheMissError, match="not in cache"):
            await http_rep.get("https://api.example.com/uncached")
