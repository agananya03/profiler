"""Pydantic v2 data models for profiler.

Responsibility: Defines Fact, Change, and Profile models with strict validation logic:
- Required source_url and retrieved_at for all facts.
- evidence_quote mandatory when method == 'llm_verified'.
- FINANCIAL_FIELDS restricted strictly to method in ('api', 'parsed').
"""

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Literal, Optional, Set, Union
from pydantic import BaseModel, Field, field_validator, model_validator

from profiler.validate import clean_orgnr, normalize_orgnr


def normalize_url(url_str: Optional[str]) -> str:
    """Defensively normalize URL string stripping Markdown link syntax if present."""
    if not url_str:
        return ""
    s = str(url_str).strip()
    match = re.search(r"\[.*?\]\(([^)]+)\)", s)
    if match:
        s = match.group(1).strip()
    s = s.lstrip("[").rstrip("]")
    if not s:
        return ""
    if not s.startswith("http://") and not s.startswith("https://"):
        s = f"https://{s}"
    return s


FINANCIAL_FIELDS: Set[str] = {
    "revenue",
    "driftsinntekter",
    "salgsinntekt",
    "operating_profit",
    "driftsresultat",
    "net_result",
    "aarsresultat",
    "årsresultat",
    "total_assets",
    "sum_eiendeler",
    "total_equity",
    "sum_egenkapital",
    "ebitda",
    "ebit",
    "profit",
    "turnover",
    "financials",
}


class Fact(BaseModel):
    """Sourced fact model with strict provenance and method validation."""

    orgnr: str
    field: str
    value: Any
    unit: Optional[str] = None
    period: Optional[str] = None
    source_name: str
    source_url: str = Field(min_length=1, description="Source URL where fact was retrieved.")
    retrieved_at: datetime = Field(description="Timestamp when fact was retrieved.")
    source_date: Optional[str] = None
    evidence_quote: Optional[str] = None
    method: Literal["api", "parsed", "llm_verified"] = Field(description="Fact acquisition method.")
    match_basis: list[str] = Field(default_factory=list, description="Proof of orgnr match.")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    content_hash: Optional[str] = None

    @field_validator("orgnr")
    @classmethod
    def validate_orgnr(cls, v: str) -> str:
        return normalize_orgnr(v)

    @field_validator("match_basis")
    @classmethod
    def validate_match_basis(cls, v: list[str]) -> list[str]:
        for mb in v:
            if mb.strip().lower() == "name_only":
                raise ValueError("Matching by name alone is strictly forbidden (Rule 5).")
        return v

    @model_validator(mode="after")
    def validate_provenance_and_method(self) -> "Fact":
        # 1. evidence_quote is required when method == "llm_verified"
        if self.method == "llm_verified":
            if not self.evidence_quote or not self.evidence_quote.strip():
                raise ValueError("evidence_quote is required when method is 'llm_verified'")

        # 2. field in FINANCIAL_FIELDS requires method in ("api", "parsed")
        if self.field.lower() in FINANCIAL_FIELDS:
            if self.method not in ("api", "parsed"):
                raise ValueError(
                    f"Field '{self.field}' is financial and requires method 'api' or 'parsed', got '{self.method}'"
                )

        # 3. Compute content_hash if missing
        if not self.content_hash:
            val_str = json.dumps(self.value, sort_keys=True) if not isinstance(self.value, str) else self.value
            raw_hash = f"{self.orgnr}:{self.field}:{val_str}:{self.source_url}"
            self.content_hash = hashlib.sha256(raw_hash.encode("utf-8")).hexdigest()

        return self


class Change(BaseModel):
    """Model representing a detected change in a company attribute."""

    field: str
    old: Any
    new: Any
    source_url: str = Field(min_length=1)
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    materiality: Literal["low", "medium", "high"] = Field(default="medium")


class Profile(BaseModel):
    """Complete company profile containing facts, null reasons, changes, and metadata."""

    orgnr: str
    status: Literal["active", "inactive", "unknown"] = Field(default="active")
    facts: list[Fact] = Field(default_factory=list)
    nulls: dict[str, str] = Field(default_factory=dict)
    changes: list[Change] = Field(default_factory=list)
    explanations: list[str] = Field(default_factory=list)
    discrepancies: list[Union[str, dict[str, Any]]] = Field(default_factory=list)
    run_meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("orgnr")
    @classmethod
    def validate_orgnr(cls, v: str) -> str:
        return normalize_orgnr(v)


# Legacy alias for backward compatibility across modules
CompanyProfile = Profile


class ErrorRecord(BaseModel):
    """Structured error log entry for adapter or system failures."""

    source: Optional[str] = None
    adapter: str
    url: Optional[str] = None
    status: Optional[int] = None
    message: str
    retry_count: int = 0
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    orgnr: Optional[str] = None
    error_type: Optional[str] = None
    timestamp: Optional[datetime] = None

    @model_validator(mode="after")
    def populate_defaults(self) -> "ErrorRecord":
        if not self.source and self.adapter:
            self.source = self.adapter
        return self


class FinancialData(BaseModel):
    """Financial statement metrics for a single accounting year."""

    year: int
    revenue: Optional[float] = None
    operating_result: Optional[float] = None
    net_result: Optional[float] = None
    total_assets: Optional[float] = None
    equity: Optional[float] = None
    total_debt: Optional[float] = None
    fiscal_year_start: Optional[str] = None
    fiscal_year_end: Optional[str] = None
    currency: Optional[str] = None
    currency_reason: Optional[str] = None
    is_consolidated: Optional[bool] = None
    consolidation_reason: Optional[str] = None
    missing_fields: list[str] = Field(default_factory=list)
    auditor_remarks: Optional[str] = None
    source: str = Field(default="Regnskapsregisteret API", description="Source of financial data")


class FinancialsProfile(BaseModel):
    """Financials section summary."""

    has_filed_accounts: bool = False
    reason_if_absent: Optional[str] = None
    latest_year_data: Optional[FinancialData] = None
    historical_data: list[FinancialData] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_absent_reason(self) -> "FinancialsProfile":
        if not self.has_filed_accounts:
            if not self.reason_if_absent:
                self.reason_if_absent = "no filed accounts found"
        return self
