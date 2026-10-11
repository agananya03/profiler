"""HTTP Client module wrapping httpx.AsyncClient with budget tracking, caching, rate limiting, and retries.

Responsibility:
- Descriptive User-Agent from PROFILER_USER_AGENT env variable.
- Every network request (including retries) counted in Budget; cache hits are NOT counted.
- Per-host token-bucket rate limiting.
- tenacity retries on 429/5xx/timeouts, honoring Retry-After.
- On-disk response cache with modes: live, record, replay. Replay mode raises CacheMissError if un-cached.
- ETag / Last-Modified conditional requests with stored validators.
- Returns Response object containing content_hash and retrieved_at.
- Raises BudgetExceeded cleanly when budget limits are breached.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, Mapping, Optional, Union
import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from profiler.budget import Budget, BudgetExceeded, BudgetTracker


class CacheMissError(Exception):
    """Raised when a request is not found in cache while running in 'replay' mode."""

    pass


class TokenBucket:
    """Token-bucket rate limiter per host."""

    def __init__(self, rate: float = 10.0, capacity: float = 10.0) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.last_update = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            if self.tokens < 1.0:
                wait_time = (1.0 - self.tokens) / self.rate
                await asyncio.sleep(wait_time)
                self.tokens = 0.0
            else:
                self.tokens -= 1.0


class WrappedResponse:
    """Wrapper around httpx.Response providing content_hash and retrieved_at."""

    def __init__(self, response: httpx.Response, retrieved_at: datetime) -> None:
        self._response = response
        self.retrieved_at = retrieved_at
        self.content_hash = hashlib.sha256(response.content).hexdigest()

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> httpx.Headers:
        return self._response.headers

    @property
    def text(self) -> str:
        return self._response.text

    @property
    def content(self) -> bytes:
        return self._response.content

    @property
    def encoding(self) -> Optional[str]:
        return getattr(self._response, "encoding", None)

    @property
    def url(self) -> Any:
        return getattr(self._response, "url", None)

    def json(self, **kwargs: Any) -> Any:
        return self._response.json(**kwargs)

    def raise_for_status(self) -> httpx.Response:
        return self._response.raise_for_status()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


class DiskCache:
    """On-disk JSON response cache."""

    def __init__(self, cache_dir: Union[str, Path] = "data/http_cache") -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_key(self, method: str, url: str, params: Optional[Mapping[str, Any]] = None) -> str:
        param_str = ""
        if params:
            param_str = json.dumps(sorted(params.items()))
        raw = f"{method.upper()}:{url}:{param_str}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, method: str, url: str, params: Optional[Mapping[str, Any]] = None) -> Optional[dict]:
        key = self._get_key(method, url, params)
        file_path = self.cache_dir / f"{key}.json"
        if not file_path.exists():
            return None
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def put(self, method: str, url: str, params: Optional[Mapping[str, Any]], data: dict) -> None:
        key = self._get_key(method, url, params)
        file_path = self.cache_dir / f"{key}.json"
        file_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class Http:
    """Shared Async HTTP Client wrapping httpx.AsyncClient."""

    def __init__(
        self,
        budget: Optional[Budget] = None,
        client: Optional[httpx.AsyncClient] = None,
        cache_mode: str = "live",  # live | record | replay
        cache_dir: Union[str, Path] = "data/http_cache",
        user_agent: Optional[str] = None,
        budget_tracker: Optional[BudgetTracker] = None,
    ) -> None:
        self.budget = budget or budget_tracker or Budget()
        self.cache_mode = cache_mode.lower()
        self.cache = DiskCache(cache_dir=cache_dir)

        ua = user_agent or os.getenv(
            "PROFILER_USER_AGENT",
            "NorwayProfiler/0.1.0 (Contact: admin@norway-profiler.no)",
        )
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": ua},
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
        )
        self._rate_limiters: Dict[str, TokenBucket] = {}
        self.per_source_request_counts: Dict[str, int] = {}
        self.per_source_failures: Dict[str, int] = {}
        self.per_source_retries: Dict[str, int] = {}
        self.per_source_backoff_seconds: Dict[str, float] = {}

    def _get_rate_limiter(self, host: str) -> TokenBucket:
        if host not in self._rate_limiters:
            self._rate_limiters[host] = TokenBucket(rate=10.0, capacity=10.0)
        return self._rate_limiters[host]

    async def close(self) -> None:
        if self._owned_client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> "Http":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        json_data: Optional[Any] = None,
        priority: str = "core",
    ) -> WrappedResponse:
        """Execute HTTP request with caching, budget tracking, rate limiting, and retries."""
        method_str = method.upper()

        # 1. Replay Mode: Check cache first (Cache hits do NOT count in budget)
        if self.cache_mode == "replay":
            cached = self.cache.get(method_str, url, params)
            if not cached:
                raise CacheMissError(f"Request '{method_str} {url}' not in cache during replay mode.")
            resp = httpx.Response(
                status_code=cached["status_code"],
                content=cached["content"].encode("utf-8"),
                headers=cached["headers"],
                request=httpx.Request(method_str, url),
            )
            retrieved_at = datetime.fromisoformat(cached["retrieved_at"])
            return WrappedResponse(resp, retrieved_at)

        # 2. Record/Live Mode: check cache for conditional headers (ETag / Last-Modified)
        cached = self.cache.get(method_str, url, params)
        req_headers = dict(headers or {})
        if cached:
            if "etag" in cached:
                req_headers["If-None-Match"] = cached["etag"]
            if "last_modified" in cached:
                req_headers["If-Modified-Since"] = cached["last_modified"]

        # Parse host for rate limiting & metrics tracking
        parsed_host = httpx.URL(url).host
        if parsed_host not in self.per_source_request_counts:
            self.per_source_request_counts[parsed_host] = 0
            self.per_source_failures[parsed_host] = 0
            self.per_source_retries[parsed_host] = 0
            self.per_source_backoff_seconds[parsed_host] = 0.0

        limiter = self._get_rate_limiter(parsed_host)

        async def _execute_attempt():
            # Every network attempt (including retries & robots.txt) counts per host
            self.per_source_request_counts[parsed_host] = self.per_source_request_counts.get(parsed_host, 0) + 1
            # Check and spend budget BEFORE every network attempt
            self.budget.spend(1, priority=priority)

            await limiter.acquire()
            response = await self._client.request(
                method_str,
                url,
                params=params,
                headers=req_headers,
                json=json_data,
            )

            # Honor Retry-After header if 429
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait_sec = float(retry_after)
                        await asyncio.sleep(wait_sec)
                    except ValueError:
                        pass
                response.raise_for_status()

            if response.status_code >= 500:
                response.raise_for_status()

            return response

        def _before_sleep_cb(retry_state):
            self.per_source_retries[parsed_host] = self.per_source_retries.get(parsed_host, 0) + 1
            sleep_sec = retry_state.next_action.sleep if retry_state.next_action else 0.0
            self.per_source_backoff_seconds[parsed_host] = round(
                self.per_source_backoff_seconds.get(parsed_host, 0.0) + float(sleep_sec), 3
            )

        # Tenacity retry logic for 429/5xx/timeouts
        retryer = AsyncRetrying(
            retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError)),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
            before_sleep=_before_sleep_cb,
            reraise=True,
        )

        try:
            async for attempt in retryer:
                with attempt:
                    resp = await _execute_attempt()
        except Exception as e:
            self.per_source_failures[parsed_host] = self.per_source_failures.get(parsed_host, 0) + 1
            raise e

        now = datetime.now(timezone.utc)

        # 3. Handle 304 Not Modified: Return cached response if conditional hit
        if resp.status_code == 304 and cached:
            cached_resp = httpx.Response(
                status_code=cached["status_code"],
                content=cached["content"].encode("utf-8"),
                headers=cached["headers"],
                request=httpx.Request(method_str, url),
            )
            return WrappedResponse(cached_resp, datetime.fromisoformat(cached["retrieved_at"]))

        # 4. Save to cache if mode is record or live
        if self.cache_mode in ("record", "live") and resp.status_code == 200:
            cache_entry = {
                "status_code": resp.status_code,
                "content": resp.text,
                "headers": dict(resp.headers),
                "retrieved_at": now.isoformat(),
            }
            if "ETag" in resp.headers:
                cache_entry["etag"] = resp.headers["ETag"]
            if "Last-Modified" in resp.headers:
                cache_entry["last_modified"] = resp.headers["Last-Modified"]

            self.cache.put(method_str, url, params, cache_entry)

        return WrappedResponse(resp, now)

    async def get(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        priority: str = "core",
    ) -> WrappedResponse:
        return await self.request("GET", url, params=params, headers=headers, priority=priority)

    async def post(
        self,
        url: str,
        *,
        json_data: Optional[Any] = None,
        headers: Optional[Mapping[str, str]] = None,
        priority: str = "core",
    ) -> WrappedResponse:
        return await self.request("POST", url, json_data=json_data, headers=headers, priority=priority)


# Alias for backward compatibility
HttpClient = Http
