"""Adapter for Brønnøysundregistrene Regnskapsregisteret API (Rule 3 & Rule 9).

Responsibility: Fetches filed annual financial statements from Regnskapsregisteret API.
Enforces Rule 3: Financial values come ONLY from Regnskapsregisteret API or parsed filed accounts.
Never estimates or computes missing financial numbers.
"""

import re
from typing import Any, List, Optional
import httpx

from profiler.adapters.base import BaseAdapter, Ctx
from profiler.models import Fact, FinancialData


class BrregRegnskapAdapter(BaseAdapter):
    """Adapter fetching filed financial statements from Regnskapsregisteret API."""

    @property
    def name(self) -> str:
        return "regnskap"

    async def fetch(self, orgnr: str, ctx: Ctx) -> List[Fact]:
        url = f"https://data.brreg.no/regnskapsregisteret/regnskap/{orgnr}"
        method = "api"
        source_name = "Regnskapsregisteret"
        match_basis = ["orgnr_exact"]
        confidence = 0.99
        facts: List[Fact] = []

        try:
            resp = await ctx.http.get(url)
            if resp.status_code == 404:
                ctx.record_error(self.name, orgnr, "NO_FILED_ACCOUNTS", "no filed accounts found")
                return []
            resp.raise_for_status()
            raw_data = resp.json()

            statements: List[dict[str, Any]] = []
            if isinstance(raw_data, list):
                statements = raw_data
            elif isinstance(raw_data, dict):
                statements = raw_data.get("regnskap", [raw_data])

            if not statements:
                ctx.record_error(self.name, orgnr, "NO_FILED_ACCOUNTS", "no filed accounts found")
                return []

            parsed_list: List[FinancialData] = []
            for stmt in statements:
                if not isinstance(stmt, dict):
                    continue
                fin_data = self._parse_statement(stmt)
                if fin_data:
                    parsed_list.append(fin_data)

            if not parsed_list:
                ctx.record_error(self.name, orgnr, "NO_FILED_ACCOUNTS", "no filed accounts found")
                return []

            # Sort by fiscal_year_end and year descending -> LATEST first
            parsed_list.sort(key=lambda f: (f.fiscal_year_end or "", f.year), reverse=True)

            for fin_data in parsed_list:
                facts.append(
                    Fact(
                        orgnr=orgnr,
                        field="financials",
                        value=fin_data.model_dump(),
                        unit=fin_data.currency,
                        period=str(fin_data.year),
                        source_name=source_name,
                        source_url=url,
                        retrieved_at=ctx.now,
                        method=method,
                        match_basis=match_basis,
                        confidence=confidence,
                        source_date=fin_data.fiscal_year_end,
                    )
                )

        except httpx.HTTPError as err:
            status_code = getattr(err.response, "status_code", 500) if hasattr(err, "response") and err.response is not None else 500
            err_body = ""
            if hasattr(err, "response") and err.response is not None and err.response.text:
                err_body = err.response.text

            is_unsupported = status_code == 500 and any(w in err_body.lower() for w in ["bank", "forsikring", "unsupported", "støttet", "stottet"])
            if is_unsupported:
                acc_type = "BANK"
                if "forsikring" in err_body.lower():
                    acc_type = "FORSIKRING"
                elif "bank" in err_body.lower():
                    acc_type = "BANK"
                else:
                    match = re.search(r"(?:format|type)\s*:?\s*([A-Z_]+)", err_body, re.IGNORECASE)
                    if match:
                        acc_type = match.group(1)
                err_msg = f"unsupported accounts format ({acc_type}) in Regnskapsregisteret"
                ctx.record_error(self.name, orgnr, "UNSUPPORTED_FORMAT", err_msg)
            else:
                msg = ""
                if hasattr(err, "response") and err.response is not None and err.response.text:
                    try:
                        err_json = err.response.json()
                        if isinstance(err_json, dict) and "message" in err_json:
                            msg = err_json["message"]
                    except Exception:
                        msg = err.response.text
                if not msg:
                    msg = str(err)
                err_msg = f"HTTP {status_code}: {msg}"
                ctx.record_error(self.name, orgnr, "HTTP_ERROR", err_msg)

        except Exception as err:
            ctx.record_error(self.name, orgnr, "UNEXPECTED_ERROR", f"fetch failed: {err}")

        return facts

    def _parse_statement(self, stmt: dict[str, Any]) -> Optional[FinancialData]:
        """Extract FinancialData model from raw Regnskapsregisteret JSON payload."""
        if not isinstance(stmt, dict):
            return None

        year = None
        fra_dato = None
        til_dato = None

        period = stmt.get("regnskapsperiode", {})
        if isinstance(period, dict):
            fra_dato = period.get("fraDato")
            til_dato = period.get("tilDato")
            if til_dato and len(str(til_dato)) >= 4 and str(til_dato)[:4].isdigit():
                year = int(str(til_dato)[:4])

        if not year and "regnskapsaar" in stmt:
            try:
                year = int(stmt["regnskapsaar"])
            except (ValueError, TypeError):
                pass

        if not year:
            return None

        # Currency
        valuta = stmt.get("valuta")
        if valuta and str(valuta).strip():
            currency = str(valuta).strip()
            currency_reason = None
        else:
            currency = None
            currency_reason = "Currency not specified in statement response"

        # Consolidation flag (KONSERN vs SELSKAP)
        regnskapstype = str(stmt.get("regnskapstype") or stmt.get("oppstillingstype") or "").upper()
        if regnskapstype == "KONSERN" or stmt.get("konsern") is True:
            is_consolidated = True
            consolidation_reason = None
        elif regnskapstype == "SELSKAP" or stmt.get("selskap") is True:
            is_consolidated = False
            consolidation_reason = None
        elif "konsern" in stmt and stmt["konsern"] is False:
            is_consolidated = False
            consolidation_reason = None
        else:
            is_consolidated = None
            consolidation_reason = "Consolidation type (KONSERN vs SELSKAP) not specified in statement response"

        # Balance Sheet - Assets
        total_assets = None
        eiendeler = stmt.get("eiendeler") or stmt.get("balanseEiendeler")
        if isinstance(eiendeler, dict):
            val = eiendeler.get("sumEiendeler")
            if isinstance(val, (int, float)):
                total_assets = float(val)

        # Balance Sheet - Equity & Debt
        equity = None
        total_debt = None
        eq_gjeld = stmt.get("egenkapitalGjeld") or stmt.get("balanseEgenkapitalGjeld")
        if isinstance(eq_gjeld, dict):
            eq_obj = eq_gjeld.get("egenkapital")
            if isinstance(eq_obj, dict):
                val = eq_obj.get("sumEgenkapital")
                if isinstance(val, (int, float)):
                    equity = float(val)

            gjeld_obj = eq_gjeld.get("gjeldOversikt") or eq_gjeld.get("gjeld")
            if isinstance(gjeld_obj, dict):
                val = gjeld_obj.get("sumGjeld")
                if isinstance(val, (int, float)):
                    total_debt = float(val)

        # Income Statement
        revenue = None
        operating_result = None
        net_result = None

        res_obj = stmt.get("resultatregnskapResultat") or stmt.get("resultatregnskap")
        if isinstance(res_obj, dict):
            raw_net = res_obj.get("aarsresultat") if res_obj.get("aarsresultat") is not None else res_obj.get("årsresultat")
            if isinstance(raw_net, (int, float)):
                net_result = float(raw_net)

            drifts_res = res_obj.get("driftsresultat")
            if isinstance(drifts_res, dict):
                raw_op = drifts_res.get("driftsresultat")
                if isinstance(raw_op, (int, float)):
                    operating_result = float(raw_op)

                drifts_innt = drifts_res.get("driftsinntekter")
                if isinstance(drifts_innt, dict):
                    raw_rev = drifts_innt.get("sumDriftsinntekter")
                    if raw_rev is None:
                        raw_rev = drifts_innt.get("salgsinntekter")
                    if raw_rev is None:
                        raw_rev = drifts_innt.get("salgsinntekt")
                    if isinstance(raw_rev, (int, float)):
                        revenue = float(raw_rev)

        metrics_map = {
            "equity": equity,
            "operating_result": operating_result,
            "total_debt": total_debt,
            "total_assets": total_assets,
            "revenue": revenue,
            "net_result": net_result,
        }
        missing_fields = [f for f, v in metrics_map.items() if v is None]

        auditor_remarks = None
        rev_info = stmt.get("revisjonsberetning") or stmt.get("revisjon")
        if isinstance(rev_info, dict):
            auditor_remarks = rev_info.get("anmerkning") or rev_info.get("beskrivelse")

        return FinancialData(
            year=year,
            revenue=revenue,
            operating_result=operating_result,
            net_result=net_result,
            total_assets=total_assets,
            equity=equity,
            total_debt=total_debt,
            fiscal_year_start=str(fra_dato) if fra_dato else None,
            fiscal_year_end=str(til_dato) if til_dato else None,
            currency=currency,
            currency_reason=currency_reason,
            is_consolidated=is_consolidated,
            consolidation_reason=consolidation_reason,
            missing_fields=missing_fields,
            auditor_remarks=auditor_remarks,
            source="Regnskapsregisteret API",
        )
