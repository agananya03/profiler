"""Unit tests for structured adapter errors, run_meta.skipped_stages, and run_report attribution."""

from pathlib import Path
import json
from unittest.mock import patch
import pytest
import respx
import httpx
from typer.testing import CliRunner

from profiler.cli import app
from profiler.adapters.base import Ctx
from profiler.adapters.website import WebsiteAdapter
from profiler.budget import BudgetTracker
from profiler.http import HttpClient, Http
from profiler.orchestrator import Orchestrator
from profiler.models import Fact, Profile


@pytest.mark.asyncio
async def test_ctx_structured_error_and_run_meta():
    """Test structured error creation in Ctx and persistence in profile.run_meta."""
    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        
        ctx.record_error(
            adapter="gleif",
            orgnr="923609016",
            error_type="HTTP_ERROR",
            message="HTTP 404: Not Found",
            url="https://api.gleif.org/api/v1/lei-records?filter[officialId]=923609016",
            status=404,
            retry_count=1,
        )

        orch = Orchestrator()
        profile = orch.compile_profile("923609016", [], ctx.errors, ctx)

        assert profile.run_meta["error_count"] == 1
        err_list = profile.run_meta["errors"]
        assert len(err_list) == 1

        err = err_list[0]
        assert err["source"] == "api.gleif.org"
        assert err["adapter"] == "gleif"
        assert err["url"] == "https://api.gleif.org/api/v1/lei-records?filter[officialId]=923609016"
        assert err["status"] == 404
        assert err["message"] == "HTTP 404: Not Found"
        assert err["retry_count"] == 1
        assert "at" in err


@pytest.mark.asyncio
async def test_run_meta_skipped_stages_reasons():
    """Test that all stages without facts get an explicit non-silent reason in run_meta.skipped_stages."""
    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        orch = Orchestrator()

        profile = orch.compile_profile("999999999", [], ctx.errors, ctx)
        skipped = profile.run_meta["skipped_stages"]

        required_stages = ["website", "llm", "gleif", "wikidata", "doffin", "financials", "kunngjoring"]
        for stage in required_stages:
            assert stage in skipped
            assert isinstance(skipped[stage], str)
            assert len(skipped[stage]) > 0

        assert skipped["kunngjoring"] == (
            "no documented public API; lifecycle status taken from Enhetsregisteret fields"
        )
        assert skipped["doffin"] == (
            "no verified keyless API; official Public API requires a subscription key"
        )


@pytest.mark.asyncio
@respx.mock
async def test_http_retries_and_robots_attribution():
    """Test that retries and robots.txt fetches are attributed to their host and counted in requests_used."""
    robots_url = "https://example.com/robots.txt"
    page_url = "https://example.com/"

    respx.get(robots_url).respond(status_code=200, text="User-agent: *\nAllow: /")
    # First attempt fails 500, second succeeds 200
    respx.get(page_url).mock(
        side_effect=[
            httpx.Response(500, text="Internal Error"),
            httpx.Response(200, text="<html><body><h1>Test Company</h1></body></html>"),
        ]
    )

    budget = BudgetTracker()
    async with Http(budget=budget) as http:
        ctx = Ctx(http=http, budget=budget)

        # Fetch robots.txt
        await http.get(robots_url)

        # Fetch page with retry
        resp = await http.get(page_url)
        assert resp.status_code == 200

        # Check attribution per host
        counts = http.per_source_request_counts
        retries = http.per_source_retries
        backoff = http.per_source_backoff_seconds

        assert counts.get("example.com") == 3  # 1 robots.txt + 2 page attempts (1 fail + 1 retry)
        assert retries.get("example.com") == 1
        assert backoff.get("example.com") > 0.0

        # Sum of per_source_request_counts equals total requests used
        assert sum(counts.values()) == budget.requests_spent


@pytest.mark.asyncio
async def test_llm_model_recorded_in_profile_and_run_meta(monkeypatch):
    """Test that llm_model is recorded in profile.run_meta when LLM_MODEL env var is set."""
    monkeypatch.setenv("LLM_MODEL", "gemini-3.6-flash")

    budget = BudgetTracker()
    async with HttpClient(budget) as http:
        ctx = Ctx(http=http, budget=budget)
        orch = Orchestrator()

        profile = orch.compile_profile("923609016", [], ctx.errors, ctx)
        assert profile.run_meta.get("llm_model") == "gemini-3.6-flash"


def test_cli_run_replay_mode_report(tmp_path: Path):
    """Test running the CLI on 2 fixture companies in replay mode and assert report exists with requests_used == sum(per_source_request_counts)."""
    runner = CliRunner()
    input_file = tmp_path / "companies.txt"
    input_file.write_text("923609016\n984851006\n", encoding="utf-8")
    out_dir = tmp_path / "out"

    result = runner.invoke(app, ["run", "--input", str(input_file), "--out", str(out_dir), "--replay"])
    assert result.exit_code == 0, f"CLI command failed:\n{result.output}"

    report_path = out_dir / "run_report.json"
    assert report_path.exists(), f"run_report.json not found in {out_dir}"

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "requests_used" in report
    assert "per_source_request_counts" in report
    assert report["requests_used"] == sum(report["per_source_request_counts"].values())
    assert report["profiles_generated"] == 2
    assert "average_fields_per_profile" in report
    assert "per_field_fill_rate" in report
    assert report["average_fields_per_profile"] > 0
    assert len(report["per_field_fill_rate"]) > 0


def test_cli_run_report_written_on_stage_failure(tmp_path: Path):
    """Test that run_report.json is always written, even if a stage fails."""
    runner = CliRunner()
    input_file = tmp_path / "companies.txt"
    input_file.write_text("923609016\n", encoding="utf-8")
    out_dir = tmp_path / "out"

    with patch.object(Orchestrator, "batch_profile", side_effect=RuntimeError("Stage failure simulated")):
        result = runner.invoke(app, ["run", "--input", str(input_file), "--out", str(out_dir), "--replay"])

    assert result.exit_code != 0
    report_path = out_dir / "run_report.json"
    assert report_path.exists(), "run_report.json must be written even when a stage fails"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["requests_used"] == sum(report["per_source_request_counts"].values())


