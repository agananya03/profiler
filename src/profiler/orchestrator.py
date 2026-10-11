"""Orchestrator for profiling execution workflow (Rules 6, 8, 9).

Responsibility:
- Async processing with a semaphore (default 20 workers).
- Per company stage execution order:
  1. Registry (BrregEnhetAdapter)
  2. Roles/accounts/announcements (BrregRollerAdapter, BrregRegnskapAdapter, BrregKunngjoringAdapter)
  3. Enrich (GleifAdapter, WikidataAdapter, DoffinAdapter)
  4. Website (WebsiteAdapter)
  5. LLM (LLMClient)
- Stops a company's later stages when its request share (budget / companies) is used up.
- Writes a checkpoint to SQLite after each company; a rerun skips completed ones (--resume).
- Failure isolation: any exception in an adapter is recorded in ctx.errors, never fatal.
- Enforces a 40-minute wall-clock stop with a clean flush.
"""

import asyncio
from datetime import datetime, timezone
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Set

from profiler.adapters.base import BaseAdapter, Ctx
from profiler.adapters.brreg import BrregEnhetAdapter
from profiler.adapters.doffin import DoffinAdapter
from profiler.adapters.gleif import GleifAdapter
from profiler.adapters.regnskap import BrregRegnskapAdapter
from profiler.adapters.roller import BrregRollerAdapter
from profiler.adapters.website import WebsiteAdapter
from profiler.adapters.wikidata import WikidataAdapter
from profiler.budget import BudgetExceededError, BudgetTracker
from profiler.http import HttpClient
from profiler.llm import LLMClient, LLMError
from profiler.models import ErrorRecord, Fact, Profile, normalize_orgnr
from profiler.resolve import check_domain
from profiler.store import Storage


class Orchestrator:
    """Main async orchestrator for company profiling runs with failure isolation and wall-clock guards."""

    def __init__(
        self,
        adapters: Optional[Sequence[BaseAdapter]] = None,
        db_path: str = ":memory:",
        budget: Optional[BudgetTracker] = None,
        http_client: Optional[HttpClient] = None,
        llm_client: Optional[LLMClient] = None,
        workers: int = 20,
        max_wall_clock_seconds: float = 2400.0,  # 40 minutes wall-clock stop
    ) -> None:
        self.db_path = db_path
        self.storage = Storage(db_path=self.db_path)
        self.budget = budget or BudgetTracker()
        self.workers = workers
        self._semaphore = asyncio.Semaphore(self.workers)
        self.max_wall_clock_seconds = max_wall_clock_seconds

        self._external_http = http_client is not None
        if http_client:
            self.http = http_client
        else:
            self.http = HttpClient(budget_tracker=self.budget)

        if llm_client is not None:
            self.llm_client = llm_client
        else:
            try:
                self.llm_client = LLMClient()
            except ValueError:
                self.llm_client = None
        self.companies_skipped_for_budget: Set[str] = set()

        # Define stage adapters
        if adapters is not None:
            self.stage_1_registry = list(adapters)
            self.stage_2_details: List[BaseAdapter] = []
            self.stage_3_enrich: List[BaseAdapter] = []
            self.stage_4_website: List[BaseAdapter] = []
        else:
            self.stage_1_registry = [BrregEnhetAdapter()]
            self.stage_2_details = [
                BrregRollerAdapter(),
                BrregRegnskapAdapter(),
            ]
            enrich_adapters: List[BaseAdapter] = [
                GleifAdapter(),
                WikidataAdapter(),
            ]
            if os.getenv("DOFFIN_API_KEY"):
                enrich_adapters.append(DoffinAdapter())
            self.stage_3_enrich = enrich_adapters
            self.stage_4_website = [WebsiteAdapter()]

    async def close(self) -> None:
        """Clean up HTTP client and storage connections."""
        if not self._external_http:
            await self.http.close()
        self.storage.close()

    async def __aenter__(self) -> "Orchestrator":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def profile_company(
        self,
        orgnr: str,
        total_companies: int = 1,
        resume: bool = False,
        start_wall_time: Optional[float] = None,
    ) -> Profile:
        """Run multi-stage profiling for a single organisation number."""
        cleaned_org = normalize_orgnr(orgnr)

        # Check resume checkpoint
        if resume:
            existing_prof = self.storage.get_profile(cleaned_org)
            if existing_prof:
                return existing_prof

        self.budget.record_company()
        start_wall = start_wall_time or time.monotonic()

        ctx = Ctx(http=self.http, budget=self.budget, llm_client=self.llm_client)
        all_facts: List[Fact] = []
        company_requests_spent = 0
        max_reqs_per_company = max(1, self.budget.max_requests // max(1, total_companies))
        skipped_stages: Dict[str, str] = {
            "kunngjoring": "no documented public API; lifecycle status taken from Enhetsregisteret fields",
        }
        if not os.getenv("DOFFIN_API_KEY"):
            skipped_stages["doffin"] = (
                "no verified keyless API; official Public API requires a subscription key"
            )

        def _sync_registry_ctx() -> None:
            registry_facts: Dict[str, Any] = getattr(ctx, "registry_facts", {})
            for f in all_facts:
                if f.source_name.lower() in ("enhetsregisteret", "brreg") or f.method == "api":
                    if f.field in ("legal_name", "name"):
                        registry_facts["legal_name"] = f.value
                    elif f.field in ("legal_form", "organization_type"):
                        registry_facts["legal_form"] = f.value
                    elif f.field == "status":
                        registry_facts["status"] = f.value
                    elif f.field == "business_address":
                        registry_facts["business_address"] = f.value
                    elif f.field == "postal_address":
                        registry_facts["postal_address"] = f.value
                    elif f.field in ("website", "hjemmeside"):
                        registry_facts["website"] = f.value
                    elif f.field == "sub_units":
                        registry_facts["sub_units"] = f.value
                if f.field == "lei":
                    registry_facts["lei"] = f.value

            setattr(ctx, "registry_facts", registry_facts)
            setattr(ctx, "orgnr", cleaned_org)
            setattr(ctx, "is_parent", bool(registry_facts.get("sub_units")))
            if "legal_name" in registry_facts:
                setattr(ctx, "legal_name", registry_facts["legal_name"])
            if "website" in registry_facts:
                setattr(ctx, "official_website", registry_facts["website"])
            if "business_address" in registry_facts and isinstance(registry_facts["business_address"], dict):
                setattr(ctx, "postal_code", registry_facts["business_address"].get("postnummer"))
            elif "postal_address" in registry_facts and isinstance(registry_facts["postal_address"], dict):
                setattr(ctx, "postal_code", registry_facts["postal_address"].get("postnummer"))

        async def _run_stage(adapter_list: List[BaseAdapter]) -> bool:
            nonlocal company_requests_spent
            for adapter in adapter_list:
                # Wall-clock 40-min stop guard
                if (time.monotonic() - start_wall) > self.max_wall_clock_seconds:
                    ctx.record_error(adapter.name, cleaned_org, "WALL_CLOCK_EXCEEDED", "40-minute wall clock limit reached.")
                    return False

                # Per-company request share guard
                if company_requests_spent >= max_reqs_per_company or not self.budget.can_afford(1, priority="core"):
                    self.companies_skipped_for_budget.add(cleaned_org)
                    ctx.record_error(adapter.name, cleaned_org, "COMPANY_BUDGET_EXCEEDED", "Company request share limit reached.")
                    return False

                reqs_before = self.budget.requests_spent
                try:
                    facts = await adapter.fetch(cleaned_org, ctx)
                    all_facts.extend(facts)
                except Exception as err:
                    # Failure isolation: never fatal
                    ctx.record_error(adapter.name, cleaned_org, "ADAPTER_ERROR", str(err))

                reqs_after = self.budget.requests_spent
                company_requests_spent += max(0, reqs_after - reqs_before)

            return True

        # Stage 1: Registry
        cont = await _run_stage(self.stage_1_registry)
        _sync_registry_ctx()

        # Stage 2: Details (roles, accounts, announcements)
        if cont:
            cont = await _run_stage(self.stage_2_details)

        # Stage 3: Enrich (GLEIF, Wikidata, Doffin)
        if cont:
            cont = await _run_stage(self.stage_3_enrich)
            _sync_registry_ctx()

        # Stage 4: Website
        if cont:
            cont = await _run_stage(self.stage_4_website)
            reg_facts = getattr(ctx, "registry_facts", {})
            if not reg_facts.get("website"):
                skipped_stages["website"] = "No official website URL found in registry facts"

        # Stage 5: LLM (only on cleaned website text if available and allowed by budget)
        if cont:
            if not self.llm_client:
                skipped_stages["llm"] = "skipped: LLM client not initialized"
            elif not getattr(self.llm_client, "api_key", None):
                skipped_stages["llm"] = "skipped: GEMINI_API_KEY environment variable not set"
            elif company_requests_spent >= max_reqs_per_company:
                skipped_stages["llm"] = "skipped: company request share limit reached"
            else:
                crawled_pages: List[Dict[str, Any]] = getattr(ctx, "crawled_pages", [])
                # Only pages that passed the resolver's domain check may be sent to the LLM (Rule 5)
                valid_pages = [
                    p for p in crawled_pages
                    if p.get("text") and len(p.get("text", "").strip()) > 30 and check_domain(p.get("url", ""), ctx)
                ]

                if not valid_pages:
                    skipped_stages["llm"] = "skipped: no page text from website crawl"
                else:
                    target_page = valid_pages[0]
                    page_text = target_page["text"]
                    page_url = target_page["url"]
                    page_match_basis = list(target_page.get("match_basis") or ["domain_match"])

                    try:
                        llm_facts = await self.llm_client.extract_facts(
                            page_text=page_text,
                            source_url=page_url,
                            orgnr=cleaned_org,
                            match_basis=page_match_basis,
                            budget=self.budget,
                        )
                        all_facts.extend(llm_facts)
                        skipped_stages["llm"] = f"ran: {len(llm_facts)} facts"
                    except LLMError as err:
                        err_msg = str(err)
                        ctx.record_error("LLMClient", cleaned_org, "LLM_ERROR", err_msg)
                        skipped_stages["llm"] = f"error: {err_msg}"
                    except Exception as err:
                        err_msg = str(err)
                        ctx.record_error("LLMClient", cleaned_org, "LLM_ERROR", err_msg)
                        skipped_stages["llm"] = f"error: {err_msg}"
        elif "llm" not in skipped_stages:
            skipped_stages["llm"] = "skipped: previous stage stopped execution"

        setattr(ctx, "skipped_stages", skipped_stages)
        profile = self.compile_profile(cleaned_org, all_facts, ctx.errors, ctx)
        self.storage.save_profile(profile)

        # Write checkpoint after each company
        self.storage.save_checkpoint("run_checkpoint", cleaned_org, "completed")
        return profile

    def compile_profile(
        self,
        orgnr: str,
        facts: List[Fact],
        errors: List[ErrorRecord],
        ctx: Optional[Any] = None,
    ) -> Profile:
        """Compile Fact list into Profile using resolution publication gate."""
        org_facts = [f for f in facts if f.orgnr == orgnr]

        registry_facts: Dict[str, Any] = {}
        for f in org_facts:
            if f.source_name.lower() in ("enhetsregisteret", "brreg") or f.method == "api":
                if f.field in ("legal_name", "name"):
                    registry_facts["legal_name"] = f.value
                elif f.field in ("legal_form", "organization_type"):
                    registry_facts["legal_form"] = f.value
                elif f.field == "status":
                    registry_facts["status"] = f.value
                elif f.field == "business_address":
                    registry_facts["business_address"] = f.value
                elif f.field == "postal_address":
                    registry_facts["postal_address"] = f.value
                elif f.field in ("website", "hjemmeside"):
                    registry_facts["website"] = f.value
                elif f.field == "sub_units":
                    registry_facts["sub_units"] = f.value

        if not ctx:
            class InternalCtx:
                pass
            ctx = InternalCtx()

        setattr(ctx, "registry_facts", registry_facts)
        setattr(ctx, "orgnr", orgnr)
        setattr(ctx, "is_parent", bool(registry_facts.get("sub_units")))
        if "legal_name" in registry_facts:
            setattr(ctx, "legal_name", registry_facts["legal_name"])
        if "website" in registry_facts:
            setattr(ctx, "official_website", registry_facts["website"])
        if "business_address" in registry_facts and isinstance(registry_facts["business_address"], dict):
            setattr(ctx, "postal_code", registry_facts["business_address"].get("postnummer"))
        elif "postal_address" in registry_facts and isinstance(registry_facts["postal_address"], dict):
            setattr(ctx, "postal_code", registry_facts["postal_address"].get("postnummer"))

        accepted_facts: List[Fact] = []
        discrepancies: List[Any] = []
        dropped_facts: List[dict] = []

        from profiler.resolve import accept

        for f in facts:
            decision = accept(f, ctx)
            if decision.accepted:
                if f.orgnr == orgnr:
                    accepted_facts.append(f)
            else:
                dropped_info = {
                    "field": f.field,
                    "value": str(f.value)[:100],
                    "reason": decision.reason,
                    "source_url": f.source_url,
                }
                dropped_facts.append(dropped_info)
                if "Rule 5" in decision.reason or "contradicts" in decision.reason.lower():
                    discrepancies.append(
                        f"Registry contradiction for field '{f.field}': value '{f.value}' from {f.source_url}"
                    )

        from profiler.explain import generate_profile_explanations
        from profiler.reconcile import reconcile_facts_with_discrepancies

        reconciled_winning_facts, fact_discrepancies = reconcile_facts_with_discrepancies(accepted_facts)
        for disc in fact_discrepancies:
            discrepancies.append(disc)

        is_bankrupt = any(f.field == "is_bankrupt" and f.value is True for f in reconciled_winning_facts)
        under_liquidation = any(f.field == "under_liquidation" and f.value is True for f in reconciled_winning_facts)
        status_fact = next((f for f in reconciled_winning_facts if f.field == "status"), None)

        if is_bankrupt or under_liquidation:
            status = "inactive"
        elif status_fact and status_fact.value in ("active", "inactive", "unknown"):
            status = status_fact.value
        elif reconciled_winning_facts:
            status = "active"
        else:
            status = "unknown"

        nulls: Dict[str, str] = {}
        has_fin = any(f.field == "financials" for f in reconciled_winning_facts)
        if not has_fin:
            regnskap_errs = [
                e for e in errors
                if (e.adapter in ("regnskap", "BrregRegnskapAdapter") or "regnskap" in e.adapter.lower())
                and e.orgnr == orgnr
            ]
            if regnskap_errs:
                err = regnskap_errs[0]
                msg = err.message
                if err.error_type == "UNSUPPORTED_FORMAT" or msg.startswith("unsupported accounts format"):
                    nulls["financials"] = msg
                elif err.error_type == "NO_FILED_ACCOUNTS" or msg == "no filed accounts found":
                    nulls["financials"] = "no filed accounts found"
                elif msg.startswith("fetch failed:"):
                    nulls["financials"] = msg
                else:
                    nulls["financials"] = f"fetch failed: {msg}"
            else:
                nulls["financials"] = "no filed accounts found"

        skipped_stages: Dict[str, str] = dict(getattr(ctx, "skipped_stages", {}))
        if "kunngjoring" not in skipped_stages:
            skipped_stages["kunngjoring"] = "no documented public API; lifecycle status taken from Enhetsregisteret fields"

        # Check facts per stage and ensure reason for every stage producing no facts
        has_fin = any(f.field == "financials" for f in reconciled_winning_facts)
        has_lei = any(f.field == "lei" or f.source_name.lower() == "gleif" for f in reconciled_winning_facts)
        has_wikidata = any(f.source_name.lower() == "wikidata" or f.field in ("qid", "wikidata_id") for f in reconciled_winning_facts)
        has_doffin = any(f.source_name.lower() == "doffin" for f in reconciled_winning_facts)
        has_website = any(f.source_name.lower() == "website" or (f.method == "parsed" and f.field in ("official_website", "schema_description")) for f in reconciled_winning_facts)
        has_llm = any(f.method == "llm_verified" for f in reconciled_winning_facts)

        if not has_fin and "financials" not in skipped_stages:
            skipped_stages["financials"] = nulls.get("financials", "no filed accounts found")

        if not has_lei and "gleif" not in skipped_stages:
            gleif_errs = [e for e in errors if "gleif" in e.adapter.lower() or (e.source and "gleif" in e.source.lower())]
            if gleif_errs:
                skipped_stages["gleif"] = gleif_errs[0].message
            else:
                skipped_stages["gleif"] = "no results for this orgnr"

        if not has_wikidata and "wikidata" not in skipped_stages:
            wd_errs = [e for e in errors if "wikidata" in e.adapter.lower() or (e.source and "wikidata" in e.source.lower())]
            if wd_errs:
                skipped_stages["wikidata"] = wd_errs[0].message
            else:
                skipped_stages["wikidata"] = "no record for this orgnr"

        if not has_doffin and "doffin" not in skipped_stages:
            if not os.getenv("DOFFIN_API_KEY"):
                skipped_stages["doffin"] = (
                    "no verified keyless API; official Public API requires a subscription key"
                )
            else:
                df_errs = [e for e in errors if "doffin" in e.adapter.lower() or (e.source and "doffin" in e.source.lower())]
                if df_errs:
                    skipped_stages["doffin"] = df_errs[0].message
                else:
                    skipped_stages["doffin"] = "no results for this orgnr"

        if not has_website and "website" not in skipped_stages:
            if not registry_facts.get("website"):
                skipped_stages["website"] = "no website in registry"
            else:
                web_errs = [e for e in errors if "website" in e.adapter.lower()]
                if web_errs:
                    skipped_stages["website"] = web_errs[0].message
                else:
                    skipped_stages["website"] = "no results for this orgnr"

        if "llm" not in skipped_stages:
            if not self.llm_client:
                skipped_stages["llm"] = "skipped: LLM client not initialized"
            elif not getattr(self.llm_client, "api_key", None):
                skipped_stages["llm"] = "skipped: GEMINI_API_KEY environment variable not set"
            else:
                skipped_stages["llm"] = "skipped: no page text from website crawl"

        temp_profile = Profile(
            orgnr=orgnr,
            status=status,
            facts=reconciled_winning_facts,
            nulls=nulls,
            changes=[],
            explanations=[],
            discrepancies=discrepancies,
            run_meta={
                "error_count": len(errors),
                "errors": [e.model_dump() for e in errors],
                "dropped_facts_count": len(dropped_facts),
                "dropped_facts": dropped_facts,
                "skipped_stages": skipped_stages,
                "llm_tokens": getattr(self.budget, "llm_tokens", 0) or 0,
                "llm_model": getattr(self.llm_client, "model_name", None) or os.getenv("LLM_MODEL"),
            },
        )

        exps_dict = generate_profile_explanations(temp_profile)
        explanations_flat = []
        for cat, items in exps_dict.items():
            explanations_flat.extend(items)

        temp_profile.explanations = explanations_flat
        return temp_profile

    async def batch_profile(self, orgnrs: List[str], resume: bool = False) -> List[Profile]:
        """Run multi-stage async profiling for a list of organisation numbers using a worker semaphore."""
        start_wall = time.monotonic()
        total_companies = len(orgnrs)
        profiles: List[Profile] = []

        async def _worker(org: str) -> Optional[Profile]:
            async with self._semaphore:
                # 40-minute wall-clock stop guard
                if (time.monotonic() - start_wall) > self.max_wall_clock_seconds:
                    return None
                try:
                    return await self.profile_company(
                        orgnr=org,
                        total_companies=total_companies,
                        resume=resume,
                        start_wall_time=start_wall,
                    )
                except BudgetExceededError:
                    self.companies_skipped_for_budget.add(org)
                    return None
                except Exception:
                    return None

        results = await asyncio.gather(*[_worker(org) for org in orgnrs])
        profiles = [p for p in results if p is not None]
        return profiles
