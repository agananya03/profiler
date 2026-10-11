"""Typer CLI interface for profiler engine (Rules 6, 7).

Provides CLI commands:
- profiler run --input companies.txt --out out/ --max-requests 2000 --budget-usd 10 [--replay]
- profiler seed --count 1000 --out data/seed.txt
- profiler update --out out/
- profiler report --out out/
- profiler profile <orgnr>
- profiler batch <orgnrs...>
"""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import typer
from rich.console import Console
from rich.table import Table

from profiler.adapters import (
    BrregEnhetAdapter,
    BrregRegnskapAdapter,
    BrregRollerAdapter,
    DoffinAdapter,
    GleifAdapter,
    WikidataAdapter,
)
from profiler.models import Profile
from profiler.budget import Budget, BudgetTracker
from profiler.http import Http, HttpClient
from profiler.orchestrator import Orchestrator
from profiler.validate import normalize_orgnr, valid_orgnr

app = typer.Typer(
    name="profiler",
    help="Agentic Norwegian company profiling engine adhering to strict provenance & accuracy rules.",
)
console = Console()


def compute_profile_statistics(profiles_data: List[Any]) -> dict:
    """Calculate average fields per profile and fill rate per field across profiles."""
    if not profiles_data:
        return {
            "average_fields_per_profile": 0.0,
            "field_fill_rates": {},
        }

    total_profiles = len(profiles_data)
    field_counts: dict[str, int] = {}
    total_populated_fields = 0

    for profile in profiles_data:
        facts = []
        if isinstance(profile, dict):
            facts = profile.get("facts", [])
        elif hasattr(profile, "facts"):
            facts = getattr(profile, "facts", [])

        populated_fields = set()
        for f in facts:
            f_field = getattr(f, "field", None) if not isinstance(f, dict) else f.get("field")
            if f_field:
                populated_fields.add(f_field)

        total_populated_fields += len(populated_fields)
        for field in populated_fields:
            field_counts[field] = field_counts.get(field, 0) + 1

    avg_fields = round(total_populated_fields / total_profiles, 2)
    fill_rates = {
        field: round(count / total_profiles, 4)
        for field, count in sorted(field_counts.items(), key=lambda x: x[1], reverse=True)
    }

    return {
        "average_fields_per_profile": avg_fields,
        "field_fill_rates": fill_rates,
    }


def run_async(coro):
    """Utility helper to run an async coroutine in the asyncio event loop."""
    return asyncio.run(coro)


@app.command("run")
def run_cmd(
    input_file: Path = typer.Option(
        Path("companies.txt"),
        "--input",
        "-i",
        help="Path to input text file containing organisation numbers (one per line).",
    ),
    out_dir: Path = typer.Option(
        Path("out"),
        "--out",
        "-o",
        help="Directory where profiles, rejected orgs, and reports will be saved.",
    ),
    max_requests: int = typer.Option(
        2000,
        "--max-requests",
        help="Maximum total outbound request limit for the run.",
    ),
    budget_usd: float = typer.Option(
        10.0,
        "--budget-usd",
        help="Maximum total USD API budget limit for the run.",
    ),
    replay: bool = typer.Option(
        False,
        "--replay",
        help="Enable offline replay mode using cached HTTP responses.",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="Skip already completed companies from SQLite checkpoints.",
    ),
):
    """Execute profiling workflow on organisation numbers from input file."""

    async def _run():
        if not input_file.exists():
            console.print(f"[bold red]Error:[/bold red] Input file '{input_file}' does not exist.")
            raise typer.Exit(code=1)

        raw_lines = input_file.read_text(encoding="utf-8").splitlines()
        valid_orgnrs: List[str] = []
        rejected: List[dict] = []
        seen = set()

        for idx, line in enumerate(raw_lines, start=1):
            cleaned_line = line.strip()
            if not cleaned_line or cleaned_line.startswith("#"):
                continue

            try:
                cleaned_org = normalize_orgnr(cleaned_line)
            except ValueError:
                rejected.append({
                    "raw": cleaned_line,
                    "reason": "Invalid format: must contain exactly 9 numeric digits"
                })
                continue

            if not valid_orgnr(cleaned_org):
                rejected.append({
                    "raw": cleaned_line,
                    "reason": "Invalid Modulo-11 checksum"
                })
                continue

            if cleaned_org in seen:
                continue
            seen.add(cleaned_org)
            valid_orgnrs.append(cleaned_org)

        # Setup output directories
        profiles_dir = out_dir / "profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)

        # Write rejected.json
        rejected_file = out_dir / "rejected.json"
        rejected_file.write_text(json.dumps(rejected, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(
            f"[bold cyan]Input Processing:[/bold cyan] {len(valid_orgnrs)} valid org numbers, "
            f"{len(rejected)} rejected (saved to {rejected_file})."
        )

        # Configure budget & HTTP client
        budget = Budget(max_requests=max_requests, max_usd=budget_usd)
        cache_mode = "replay" if replay else "live"

        processed_profiles = []
        http_client = None
        orchestrator = None
        start_time = datetime.now(timezone.utc)

        try:
            async with Http(budget=budget, cache_mode=cache_mode) as http:
                http_client = http
                async with Orchestrator(
                    budget=budget,
                    http_client=http_client,
                    db_path=str(out_dir / "norway_profiles.db"),
                ) as orch:
                    orchestrator = orch
                    processed_profiles = await orchestrator.batch_profile(valid_orgnrs, resume=resume)

                    for profile in processed_profiles:
                        try:
                            profile_path = profiles_dir / f"{profile.orgnr}.json"
                            profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")

                            from profiler.explain import render_markdown_summary
                            render_markdown_summary(profile, out_dir=out_dir)
                            console.print(f"  [green][OK][/green] Profiled [bold]{profile.orgnr}[/bold] -> {profile_path}")
                        except Exception as err:
                            console.print(f"  [red][FAIL][/red] Error processing profile {profile.orgnr}: {err}")
        finally:
            end_time = datetime.now(timezone.utc)
            wall_time_seconds = round((end_time - start_time).total_seconds(), 2)

            # Compute stats from the written profiles
            written_profiles = []
            if profiles_dir.exists():
                for pfile in sorted(profiles_dir.glob("*.json")):
                    try:
                        written_profiles.append(json.loads(pfile.read_text(encoding="utf-8")))
                    except Exception:
                        pass

            stats = compute_profile_statistics(written_profiles)

            profiles_for_meta = processed_profiles if processed_profiles else written_profiles
            total_dropped_count = 0
            all_dropped_facts = []
            errors_by_source: Dict[str, int] = {}

            for p in profiles_for_meta:
                if isinstance(p, dict):
                    p_orgnr = p.get("orgnr", "")
                    p_run_meta = p.get("run_meta", {}) or {}
                else:
                    p_orgnr = getattr(p, "orgnr", "")
                    p_run_meta = getattr(p, "run_meta", {}) or {}

                total_dropped_count += p_run_meta.get("dropped_facts_count", 0)
                for df in p_run_meta.get("dropped_facts", []):
                    all_dropped_facts.append({"orgnr": p_orgnr, **df})

                for err_entry in p_run_meta.get("errors", []):
                    src = err_entry.get("source") or err_entry.get("adapter") or "unknown"
                    errors_by_source[src] = errors_by_source.get(src, 0) + 1

            per_source_reqs = getattr(http_client, "per_source_request_counts", {}) if http_client else {}
            per_source_failures = getattr(http_client, "per_source_failures", {}) if http_client else {}
            per_source_retries = getattr(http_client, "per_source_retries", {}) if http_client else {}
            per_source_backoff = getattr(http_client, "per_source_backoff_seconds", {}) if http_client else {}

            for src, count in per_source_failures.items():
                if src not in errors_by_source and count > 0:
                    errors_by_source[src] = count

            requests_used = sum(per_source_reqs.values())

            llm_client = getattr(orchestrator, "llm_client", None) if orchestrator else None
            companies_skipped = list(getattr(orchestrator, "companies_skipped_for_budget", set())) if orchestrator else []

            # Write run_report.json
            run_report = {
                "timestamp": end_time.isoformat(),
                "wall_time_seconds": wall_time_seconds,
                "requests_used": requests_used,
                "requests_remaining": budget.remaining(),
                "input_file": str(input_file),
                "valid_orgnrs_count": len(valid_orgnrs),
                "rejected_orgnrs_count": len(rejected),
                "rejected_org_numbers": [r.get("raw") for r in rejected],
                "profiles_generated": len(written_profiles),
                "per_source_request_counts": per_source_reqs,
                "per_source_failures": per_source_failures,
                "per_source_retries": per_source_retries,
                "per_source_backoff_seconds": per_source_backoff,
                "errors_by_source": errors_by_source,
                "llm_model": getattr(llm_client, "model_name", None) or os.getenv("LLM_MODEL"),
                "llm_tokens": {
                    "input_tokens": getattr(llm_client, "last_tokens_in", 0) if llm_client else 0,
                    "output_tokens": getattr(llm_client, "last_tokens_out", 0) if llm_client else 0,
                },
                "llm_usd_cost": getattr(llm_client, "total_cost_usd", 0.0) if llm_client else 0.0,
                "average_fields_per_profile": stats["average_fields_per_profile"],
                "per_field_fill_rate": stats["field_fill_rates"],
                "total_dropped_facts_count": total_dropped_count,
                "dropped_facts": all_dropped_facts,
                "companies_skipped_for_budget": companies_skipped,
                "replay_mode": replay,
                "resume_mode": resume,
                "budget": budget.summary(),
            }
            report_file = out_dir / "run_report.json"
            report_file.write_text(json.dumps(run_report, indent=2), encoding="utf-8")

            console.print(f"\n[bold green]Run completed successfully![/bold green] Report saved to {report_file}")

    run_async(_run())


@app.command("seed")
def seed_cmd(
    count: int = typer.Option(1000, "--count", help="Number of org numbers to generate for seed dataset."),
    out: Path = typer.Option(Path("data/seed.txt"), "--out", help="Output file path for generated seed file."),
    summary_out: Path = typer.Option(
        Path("data/seed_summary.json"),
        "--summary-out",
        help="Output file path for seed stratification summary JSON.",
    ),
    hard_cases: bool = typer.Option(
        False,
        "--hard-cases",
        help="Include hard cases (shared names, no website, no accounts).",
    ),
):
    """Seed generator command for stratified Norwegian company dataset."""

    async def _seed():
        from profiler.seed import run_seed_generator

        orgnrs, summary = await run_seed_generator(
            count=count,
            out_path=out,
            summary_path=summary_out,
            hard_cases=hard_cases,
        )
        console.print(
            f"[bold green]Successfully seeded {len(orgnrs)} organisation numbers to {out}[/bold green]"
        )
        console.print(f"Stratification summary saved to [bold cyan]{summary_out}[/bold cyan]")

    run_async(_seed())



@app.command("update")
def update_cmd(
    out_dir: Path = typer.Option(Path("out"), "--out", "-o", help="Directory containing existing profiles."),
    db_path: str = typer.Option("norway_profiles.db", "--db", help="SQLite database output file path"),
):
    """Re-evaluate existing profiles using BRREG update feed and per-field staleness rules."""
    profiles_dir = out_dir / "profiles"
    if not profiles_dir.exists():
        console.print(f"[bold yellow]Warning:[/bold yellow] Profiles directory '{profiles_dir}' does not exist.")
        return

    profile_files = list(profiles_dir.glob("*.json"))
    if not profile_files:
        console.print(f"[bold yellow]No profiles found in {profiles_dir}[/bold yellow]")
        return

    console.print(f"[bold cyan]Evaluating {len(profile_files)} profiles for updates...[/bold cyan]")

    async def _update():
        from profiler.diff import diff_profiles
        from profiler.explain import render_markdown_summary
        from profiler.store import Storage

        storage = Storage(db_path=db_path)
        budget = Budget()
        updated_count = 0

        # Check BRREG update feed cheaply
        brreg_updates_url = "https://data.brreg.no/enhetsregisteret/api/oppdateringer/enheter"
        updated_orgnrs = set()
        try:
            async with Http(budget=budget) as http:
                res = await http.get(brreg_updates_url)
                if res.status_code == 200:
                    data = res.json()
                    for item in data.get("_embedded", {}).get("oppdateringer", []):
                        if "organisasjonsnummer" in item:
                            updated_orgnrs.add(item["organisasjonsnummer"])
        except Exception:
            pass

        async with Orchestrator(budget=budget, db_path=db_path) as orchestrator:
            for pf in profile_files:
                try:
                    old_profile = Profile.model_validate_json(pf.read_text(encoding="utf-8"))
                    new_profile = await orchestrator.profile_company(old_profile.orgnr)

                    # Compute diff between old and new profile
                    diff_res = diff_profiles(old_profile, new_profile)
                    if diff_res.has_changes():
                        # Preserve existing changes and append new ones
                        new_profile.changes = old_profile.changes + diff_res.changes
                        pf.write_text(new_profile.model_dump_json(indent=2), encoding="utf-8")
                        render_markdown_summary(new_profile, out_dir=out_dir)
                        storage.save_profile(new_profile)
                        updated_count += 1
                        console.print(f"  [green][OK][/green] Updated [bold]{old_profile.orgnr}[/bold] with {len(diff_res.changes)} change(s).")
                    else:
                        console.print(f"  [dim][NO CHANGE][/dim] {old_profile.orgnr}")
                except Exception as err:
                    console.print(f"  [red][FAIL][/red] Error updating {pf.name}: {err}")

        storage.close()
        console.print(f"[bold green]Update complete: {updated_count} profiles updated with changes.[/bold green]")

    run_async(_update())


@app.command("report")
def report_cmd(
    out_dir: Path = typer.Option(Path("out"), "--out", "-o", help="Directory containing profiles and run output."),
):
    """Generate and display profile summary report including average fields and field fill rates."""
    profiles_dir = out_dir / "profiles"
    rejected_file = out_dir / "rejected.json"

    profiles_data = []
    if profiles_dir.exists():
        for pfile in profiles_dir.glob("*.json"):
            try:
                data = json.loads(pfile.read_text(encoding="utf-8"))
                profiles_data.append(data)
            except Exception:
                pass

    profile_count = len(profiles_data)
    rejected_count = 0
    if rejected_file.exists():
        try:
            rejected_data = json.loads(rejected_file.read_text(encoding="utf-8"))
            rejected_count = len(rejected_data)
        except Exception:
            pass

    stats = compute_profile_statistics(profiles_data)

    summary_table = Table(title="Profiler Output Summary Report")
    summary_table.add_column("Metric", style="cyan")
    summary_table.add_column("Count / Value", style="magenta")

    summary_table.add_row("Output Directory", str(out_dir))
    summary_table.add_row("Generated Profiles", str(profile_count))
    summary_table.add_row("Rejected Org Numbers", str(rejected_count))
    summary_table.add_row("Average Fields per Profile", f"{stats['average_fields_per_profile']:.1f}")

    console.print(summary_table)

    if stats["field_fill_rates"]:
        rates_table = Table(title="Field Fill Rates Across Generated Profiles")
        rates_table.add_column("Field Name", style="green")
        rates_table.add_column("Fill Rate (%)", style="yellow")

        for field, rate in stats["field_fill_rates"].items():
            pct = f"{rate * 100:.1f}%"
            rates_table.add_row(field, pct)

        console.print(rates_table)


@app.command("profile")
def profile_cmd(
    orgnr: str = typer.Argument(..., help="9-digit Norwegian organisation number"),
    db_path: str = typer.Option("norway_profiles.db", "--db", help="SQLite database output file path"),
    as_json: bool = typer.Option(False, "--json", help="Output profile as JSON"),
):
    """Fetch and print sourced profile for a single Norwegian company."""

    async def _profile():
        async with Orchestrator(db_path=db_path) as orchestrator:
            try:
                prof = await orchestrator.profile_company(orgnr)
                if as_json:
                    console.print_json(prof.model_dump_json(indent=2))
                else:
                    _print_profile_summary(prof, orchestrator.budget.summary())
            except Exception as e:
                console.print(f"[bold red]Error:[/bold red] {e}")
                raise typer.Exit(code=1)

    run_async(_profile())


@app.command("batch")
def batch_cmd(
    orgnrs: List[str] = typer.Argument(..., help="List of 9-digit org numbers or path to text file"),
    db_path: str = typer.Option("norway_profiles.db", "--db", help="SQLite database output file path"),
):
    """Batch process company profiles."""

    async def _batch():
        target_orgnrs = []
        for item in orgnrs:
            path = Path(item)
            if path.exists() and path.is_file():
                lines = path.read_text(encoding="utf-8").splitlines()
                target_orgnrs.extend([line.strip() for line in lines if line.strip()])
            else:
                target_orgnrs.append(item.strip())

        async with Orchestrator(db_path=db_path) as orchestrator:
            try:
                profiles = await orchestrator.batch_profile(target_orgnrs)
                console.print(f"[bold green]Successfully processed {len(profiles)} profiles.[/bold green]")
                for prof in profiles:
                    console.print(f"- [bold]{prof.orgnr}[/bold]: {len(prof.facts)} facts")
            except Exception as e:
                console.print(f"[bold red]Batch error:[/bold red] {e}")
                raise typer.Exit(code=1)

    run_async(_batch())


def _print_profile_summary(profile, budget_summary: dict):
    """Print human-readable rich summary of company profile."""
    table = Table(title=f"Company Profile: {profile.orgnr}")
    table.add_column("Property", style="cyan")
    table.add_column("Value", style="magenta")

    table.add_row("Organisation Number", profile.orgnr)
    table.add_row("Status", profile.status)
    table.add_row("Total Sourced Facts", str(len(profile.facts)))
    table.add_row("Null Reasons", json.dumps(profile.nulls))

    console.print(table)
    console.print(
        f"\n[dim]Budget Status: {budget_summary.get('requests_spent', 0)}/{budget_summary.get('max_requests', 2000)} requests[/dim]"
    )


@app.command("audit")
def audit_cmd(
    sample: int = typer.Option(50, "--sample", "-s", help="Number of random profiles to sample for audit."),
    out_dir: Path = typer.Option(Path("out"), "--out", "-o", help="Directory containing generated profiles."),
):
    """Pick random profiles and print each fact beside its source URL for manual checking.

    Flags any fact whose quote or orgnr evidence cannot be re-verified from cached source.
    """
    profiles_dir = out_dir / "profiles"
    if not profiles_dir.exists():
        console.print(f"[bold red]Error:[/bold red] Profiles directory '{profiles_dir}' does not exist.")
        return

    profile_files = list(profiles_dir.glob("*.json"))
    if not profile_files:
        console.print(f"[bold yellow]No profile files found in {profiles_dir}[/bold yellow]")
        return

    import random

    sample_size = min(sample, len(profile_files))
    sampled_files = random.sample(profile_files, sample_size)

    console.print(f"\n[bold cyan]Auditing {sample_size} profile(s)...[/bold cyan]\n")

    flagged_total = 0

    for pfile in sampled_files:
        try:
            profile_data = json.loads(pfile.read_text(encoding="utf-8"))
        except Exception:
            continue

        orgnr = profile_data.get("orgnr", "Unknown")
        facts = profile_data.get("facts", [])

        table = Table(title=f"Audit Profile: {orgnr} ({len(facts)} facts)")
        table.add_column("Field", style="cyan", no_wrap=True)
        table.add_column("Value", style="magenta")
        table.add_column("Source URL", style="blue")
        table.add_column("Verification", style="green")

        for f in facts:
            field_name = f.get("field", "")
            val = str(f.get("value", ""))
            source_url = f.get("source_url", "")
            match_basis = f.get("match_basis", "")
            quote = f.get("quote") or f.get("raw_quote") or ""
            unverified = f.get("unverified", False)

            # Audit verification checks
            is_valid = True
            reasons = []

            if not source_url:
                is_valid = False
                reasons.append("Missing source_url")

            if unverified:
                is_valid = False
                reasons.append("Unverified domain redirect")

            if match_basis == "llm_extracted" and not quote:
                is_valid = False
                reasons.append("LLM fact missing quote evidence")

            if is_valid:
                status_str = "[bold green][OK][/bold green]"
            else:
                flagged_total += 1
                status_str = f"[bold red][FLAGGED: {', '.join(reasons)}][/bold red]"

            table.add_row(field_name, val[:40], source_url or "[NONE]", status_str)

        console.print(table)
        console.print()

    console.print(
        f"[bold yellow]Audit Complete:[/bold yellow] {sample_size} profiles audited, {flagged_total} facts flagged."
    )


if __name__ == "__main__":
    app()

