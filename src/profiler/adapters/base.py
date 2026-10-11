"""Base adapter protocol and execution context module (Rules 8, 9).

Responsibility: Defines the Ctx execution context and Adapter protocol / BaseAdapter ABC.
Adapters NEVER raise exceptions to the orchestrator; they return valid facts and record errors in `ctx.record_error`.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import logging
from typing import Any, List, Optional, Protocol, runtime_checkable

from profiler.models import ErrorRecord, Fact

logger = logging.getLogger("profiler.adapters")


class Ctx:
    """Execution context passed to adapters carrying HTTP client, budget, logger, and error log."""

    def __init__(
        self,
        http: Any,
        budget: Any,
        now: Optional[datetime] = None,
        logger_instance: Optional[logging.Logger] = None,
        llm_client: Any = None,
    ) -> None:
        self.http = http
        self.budget = budget
        self.now = now or datetime.now(timezone.utc)
        self.logger = logger_instance or logger
        self.llm_client = llm_client
        self.errors: List[ErrorRecord] = []
        self.skipped_stages: dict[str, str] = {}

    def record_error(
        self,
        adapter: str,
        orgnr: str = "",
        error_type: Optional[str] = None,
        message: str = "",
        *,
        source: Optional[str] = None,
        url: Optional[str] = None,
        status: Optional[int] = None,
        retry_count: int = 0,
        at: Optional[str] = None,
    ) -> ErrorRecord:
        """Record an error in the execution context without throwing an exception."""
        now_iso = at or self.now.isoformat()

        derived_source = source
        if not derived_source and url:
            try:
                import httpx
                derived_source = httpx.URL(url).host
            except Exception:
                derived_source = adapter
        if not derived_source:
            derived_source = adapter

        err = ErrorRecord(
            source=derived_source,
            adapter=adapter,
            url=url,
            status=status,
            message=message or (error_type or "ADAPTER_ERROR"),
            retry_count=retry_count,
            at=now_iso,
            orgnr=orgnr,
            error_type=error_type,
            timestamp=self.now,
        )
        self.errors.append(err)
        self.logger.warning(f"[{adapter}] Error for {orgnr} ({error_type or 'ERROR'}): {message}")
        return err


@runtime_checkable
class Adapter(Protocol):
    """Protocol for profiler data source adapters."""

    @property
    def name(self) -> str:
        """Unique adapter name identifier."""
        ...

    async def fetch(self, orgnr: str, ctx: Ctx) -> List[Fact]:
        """Fetch facts for organisation number without raising exceptions."""
        ...


class BaseAdapter(ABC):
    """Abstract base class for all profiler adapters."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique adapter name identifier."""
        pass

    @abstractmethod
    async def fetch(self, orgnr: str, ctx: Ctx) -> List[Fact]:
        """Fetch facts for organisation number without raising exceptions to orchestrator."""
        pass
