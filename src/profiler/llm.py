"""LLM extraction module with Gemini API, prompt scoping, caching, and quote verification (Rule 3 & Rule 4).

Responsibility:
- Single LLM call per company on cleaned page text truncated to ~6k tokens (~24,000 chars).
- Uses Gemini API with model defined in env variable LLM_MODEL (default "gemini-2.5-flash").
- Strictly limits field requests to: description, products_services, customer_segments, founded_claim, certifications.
- NEVER asks for financials, people/executives, or company size numbers.
- Code-level quote verification (verify_quote):
  (a) quote is a substring of page_text (normalized whitespace and case).
  (b) 40 <= len(quote) <= 300 characters.
  (c) key tokens of value appear in the quote.
- SHA-256 caching of (page_text + prompt_version).
- Budget tracking for tokens and USD costs.
"""

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import random
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union
import httpx

from profiler.budget import BudgetTracker
from profiler.models import FINANCIAL_FIELDS, Fact

PROMPT_VERSION = "v1.1"

class LLMError(Exception):
    """Raised on LLM API or candidate extraction failure."""
    pass


DEFAULT_PRICING: Dict[str, Dict[str, float]] = {
    # USD per 1M tokens
    "gemini-2.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-3.6-flash": {"input": 0.075, "output": 0.30},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00},
    "default": {"input": 0.075, "output": 0.30},
}


# Allowed & Forbidden fields
ALLOWED_LLM_FIELDS: Set[str] = {
    "description",
    "products_services",
    "customer_segments",
    "founded_claim",
    "certifications",
}

FORBIDDEN_LLM_FIELDS: Set[str] = set(FINANCIAL_FIELDS).union(
    {
        "employee_count",
        "employees",
        "headcount",
        "revenue",
        "profit",
        "ceo",
        "board",
        "management",
        "shares",
        "salary",
    }
)

STOPWORDS: Set[str] = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "when", "at", "by", "for", "with",
    "about", "against", "between", "into", "through", "during", "before", "after", "above", "below",
    "to", "from", "up", "upon", "down", "in", "out", "on", "off", "over", "under", "again", "further",
    "then", "once", "here", "there", "when", "where", "why", "how", "all", "any", "both", "each",
    "few", "more", "most", "other", "some", "such", "no", "nor", "not", "only", "own", "same", "so",
    "than", "too", "very", "can", "will", "just", "should", "now", "og", "i", "på", "for", "med",
    "av", "til", "er", "som", "det", "en", "et", "den", "de", "vi", "har", "om", "men", "eller"
}

SYSTEM_PROMPT = """You are a precise corporate data extraction assistant.
Given text from a company's official website, extract ONLY factual claims for the following allowed fields:
- description: Concise summary of what the company does.
- products_services: Specific products or services offered.
- customer_segments: Target industries, client types, or market segments served.
- founded_claim: Claimed year or story of foundation.
- certifications: ISO standards, environmental, security, or industry accreditations.

STRICT SAFETY RULES:
1. NEVER extract or mention financial metrics (revenue, profit, equity, debt, assets).
2. NEVER extract or mention names of people, executives, or board members.
3. NEVER extract numbers or statistics about company size, headcount, or employee count.
4. For EVERY extracted field, you MUST provide an exact verbatim quote from the page text.
5. The quote MUST be between 40 and 300 characters long and MUST contain the value.

Respond ONLY with a valid JSON array of objects, with no markdown codeblocks or extra prose:
[
  {
    "field": "description",
    "value": "...",
    "quote": "..."
  }
]
If no valid facts are found, return an empty JSON array: []
"""


def normalize_whitespace(text: str) -> str:
    """Replace multiple whitespace characters with a single space and strip."""
    return re.sub(r"\s+", " ", text).strip()


def get_key_tokens(value: Any) -> List[str]:
    """Extract key tokens (words >= 3 chars, lowercased, excluding stopwords) from value."""
    val_str = str(value).strip().lower()
    if not val_str:
        return []
    words = re.findall(r"\b[a-z0-9æøå_]{3,}\b", val_str)
    key_words = [w for w in words if w not in STOPWORDS]
    if not key_words:
        key_words = re.findall(r"\b[a-z0-9æøå_]+\b", val_str)
    return key_words


def verify_quote(quote: str, page_text: str, value: Any) -> bool:
    """Verify LLM evidence quote against page text and value (Rule 4).

    Requirements:
    (a) Quote is a substring of the page text (normalized whitespace and case).
    (b) Quote length is between 40 and 300 characters inclusive.
    (c) Key tokens of value appear in the quote.
    """
    if not quote or not page_text or not isinstance(quote, str) or not isinstance(page_text, str):
        return False

    raw_quote_len = len(quote.strip())
    if not (40 <= raw_quote_len <= 300):
        return False

    quote_norm = normalize_whitespace(quote).lower()
    page_norm = normalize_whitespace(page_text).lower()

    if quote_norm not in page_norm:
        return False

    key_tokens = get_key_tokens(value)
    if not key_tokens:
        return False

    for token in key_tokens:
        if token not in quote_norm:
            return False

    return True


def is_verbatim_quote_in_text(quote: str, text: str) -> bool:
    """Check if quote appears verbatim in text (allowing normalized whitespace match)."""
    if not quote or not text:
        return False
    if quote in text:
        return True
    return normalize_whitespace(quote) in normalize_whitespace(text)


def contains_value_str(extracted_val: Any, quote: str) -> bool:
    """Check if extracted value representation appears in quote."""
    val_str = str(extracted_val).strip()
    if not val_str:
        return False
    if val_str.lower() in quote.lower():
        return True
    val_norm = normalize_whitespace(val_str)
    return val_norm.lower() in normalize_whitespace(quote).lower()


def validate_llm_fact(
    field: str,
    extracted_value: Any,
    evidence_quote: Optional[str],
    page_text: str,
) -> bool:
    """Validate that LLM-extracted fact complies with Rule 3 and Rule 4."""
    field_lower = field.lower()
    if field_lower in FINANCIAL_FIELDS or field_lower in FORBIDDEN_LLM_FIELDS:
        return False

    if not evidence_quote or not isinstance(evidence_quote, str):
        return False

    if not verify_quote(evidence_quote, page_text, extracted_value):
        if not is_verbatim_quote_in_text(evidence_quote, page_text):
            return False
        if not contains_value_str(extracted_value, evidence_quote):
            return False
    return True


def get_llm_model() -> str:
    """Get LLM model name from LLM_MODEL environment variable.

    Raises:
        ValueError: If LLM_MODEL environment variable is unset or empty.
    """
    model = os.getenv("LLM_MODEL")
    if not model or not model.strip():
        raise ValueError("LLM_MODEL environment variable must be set (e.g. LLM_MODEL=gemini-3.6-flash).")
    return model.strip()


def parse_retry_after(header_val: Optional[str]) -> Optional[float]:
    """Parse HTTP Retry-After header as seconds (float) or HTTP-date."""
    if not header_val:
        return None
    val_str = header_val.strip()
    try:
        val = float(val_str)
        return max(0.0, val)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(val_str)
        now = datetime.now(timezone.utc)
        return max(0.0, (dt - now).total_seconds())
    except Exception:
        return None


def get_llm_fallback_model() -> Optional[str]:
    """Get fallback LLM model name from LLM_FALLBACK_MODEL environment variable if set."""
    model = os.getenv("LLM_FALLBACK_MODEL")
    if model and model.strip():
        return model.strip()
    return None


class LLMClient:
    """LLM Client for company fact extraction using Gemini API with caching and quote verification."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        fallback_model: Optional[str] = None,
        cache_dir: str = "data/llm_cache",
        pricing: Optional[Dict[str, Dict[str, float]]] = None,
        jitter: bool = True,
    ) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model_name = model_name or get_llm_model()
        self.fallback_model = fallback_model or get_llm_fallback_model()
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.discarded_count = 0
        self.pricing = pricing or DEFAULT_PRICING
        self.total_tokens_in = 0
        self.total_tokens_out = 0
        self.last_tokens_in = 0
        self.last_tokens_out = 0
        self.total_cost_usd = 0.0
        self.last_usage_metadata: dict = {}
        self.retry_count = 0
        self.total_backoff_seconds = 0.0
        self.models_used: Dict[str, int] = {}
        self.llm_models_used = self.models_used
        self.last_model_used: Optional[str] = None
        self.jitter = jitter

    def _compute_cache_key(self, page_text: str) -> str:
        content = f"{self.model_name}:{PROMPT_VERSION}:{page_text}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def _sleep(self, seconds: float) -> None:
        """Asynchronous sleep wrapper."""
        await asyncio.sleep(seconds)

    async def _send_api_request(self, model: str, payload: dict) -> httpx.Response:
        """Execute a single HTTP POST request to the Gemini generateContent endpoint."""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }
        async with httpx.AsyncClient(timeout=30.0) as http:
            return await http.post(url, json=payload, headers=headers)

    def _parse_gemini_response(self, res: httpx.Response, model_used: str) -> str:
        """Extract and validate candidate text from a successful 200 OK Gemini response."""
        data = res.json()
        self.last_usage_metadata = data.get("usageMetadata", {})

        candidates = data.get("candidates", [])
        if not candidates:
            prompt_feedback = data.get("promptFeedback")
            raise LLMError(f"Missing candidates: promptFeedback={prompt_feedback}")

        first_cand = candidates[0]
        parts = first_cand.get("content", {}).get("parts", [])
        if not parts or "text" not in parts[0]:
            finish_reason = first_cand.get("finishReason")
            prompt_feedback = data.get("promptFeedback")
            raise LLMError(f"Missing candidate text: finishReason={finish_reason}, promptFeedback={prompt_feedback}")

        self.last_model_used = model_used
        self.models_used[model_used] = self.models_used.get(model_used, 0) + 1
        return parts[0]["text"]

    async def _call_gemini_api(self, prompt: str, page_text: str) -> str:
        """Call Gemini generateContent endpoint with retries on 429/5xx and fallback model support."""
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY environment variable not set")

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": f"{prompt}\n\nWebsite Content:\n{page_text}"}
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.0,
            },
        }

        # Backoff schedule: 2s, 4s, 8s
        backoff_schedule = [2.0, 4.0, 8.0]
        max_retries = len(backoff_schedule)  # Up to 3 retries (4 attempts total)
        failed_statuses: List[int] = []
        last_error_text = ""

        # Primary model attempts
        for attempt in range(max_retries + 1):
            try:
                res = await self._send_api_request(self.model_name, payload)
            except httpx.HTTPError as err:
                last_error_text = f"HTTP request failed: {err}"
                if attempt == max_retries:
                    raise LLMError(last_error_text) from err
                delay = backoff_schedule[attempt] + (random.uniform(0.01, 0.25) if self.jitter else 0.0)
                self.retry_count += 1
                self.total_backoff_seconds += delay
                await self._sleep(delay)
                continue

            last_status = res.status_code
            last_error_text = res.text[:300]

            if last_status == 200:
                return self._parse_gemini_response(res, self.model_name)

            # Rule 4: Do not retry on 400 or 403, raise LLMError immediately
            if last_status in {400, 403}:
                raise LLMError(f"HTTP {last_status}: {last_error_text}")

            # Retry on 429, 500, 502, 503, 504
            if last_status in {429, 500, 502, 503, 504}:
                failed_statuses.append(last_status)
                if attempt < max_retries:
                    retry_after = parse_retry_after(res.headers.get("Retry-After"))
                    if retry_after is not None:
                        delay = retry_after
                    else:
                        delay = backoff_schedule[attempt] + (random.uniform(0.01, 0.25) if self.jitter else 0.0)

                    self.retry_count += 1
                    self.total_backoff_seconds += delay
                    await self._sleep(delay)
                    continue
                else:
                    # Retries exhausted on primary model
                    break
            else:
                # Any other unexpected non-200 status
                raise LLMError(f"HTTP {last_status}: {last_error_text}")

        # Check if all retries on the primary model failed with 503 or 429
        all_retries_503_or_429 = bool(
            failed_statuses
            and (
                all(s in {429, 503} for s in failed_statuses)
                or all(s in {429, 503} for s in failed_statuses[1:])
            )
        )

        if all_retries_503_or_429:
            fallback = self.fallback_model or get_llm_fallback_model()
            if fallback and fallback.strip():
                fallback_model = fallback.strip()
                try:
                    res_fallback = await self._send_api_request(fallback_model, payload)
                    if res_fallback.status_code == 200:
                        return self._parse_gemini_response(res_fallback, fallback_model)
                    raise LLMError(
                        f"Fallback model '{fallback_model}' failed with HTTP {res_fallback.status_code}: {res_fallback.text[:300]}"
                    )
                except httpx.HTTPError as err:
                    raise LLMError(f"Fallback model '{fallback_model}' HTTP request failed: {err}") from err

        final_status = failed_statuses[-1] if failed_statuses else "error"
        raise LLMError(f"HTTP {final_status}: {last_error_text}")

    async def extract_facts(
        self,
        page_text: str,
        source_url: str,
        orgnr: str,
        match_basis: List[str],
        budget: BudgetTracker,
    ) -> List[Fact]:
        """Extract non-financial company facts with mandatory verbatim quote verification."""
        # 1. Truncate clean page text to ~6k tokens (~24,000 characters)
        truncated_text = page_text[:24_000]

        # 2. Check disk cache
        cache_key = self._compute_cache_key(truncated_text)
        cache_file = self.cache_dir / f"{cache_key}.json"

        extracted_items = None
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
                cached_model = cached_data.get("model")
                valid_models = {self.model_name}
                fallback_env = self.fallback_model or get_llm_fallback_model()
                if fallback_env:
                    valid_models.add(fallback_env.strip())
                if (
                    cached_data.get("prompt_version") == PROMPT_VERSION
                    and (cached_model in valid_models or cached_model == self.model_name)
                    and "extracted_items" in cached_data
                ):
                    extracted_items = cached_data["extracted_items"]
                    self.last_model_used = cached_model
            except Exception:
                extracted_items = None

        if extracted_items is None:
            # 3. Call LLM API (at most once per company)
            raw_response = await self._call_gemini_api(SYSTEM_PROMPT, truncated_text)
            clean_raw = raw_response.strip()
            if clean_raw.startswith("```json"):
                clean_raw = clean_raw[7:]
            if clean_raw.startswith("```"):
                clean_raw = clean_raw[3:]
            if clean_raw.endswith("```"):
                clean_raw = clean_raw[:-3]
            try:
                parsed = json.loads(clean_raw.strip())
                if isinstance(parsed, list):
                    extracted_items = parsed
                else:
                    raise LLMError(f"Invalid JSON response structure: expected list, got {type(parsed)}")
            except json.JSONDecodeError as err:
                raise LLMError(f"Failed to decode LLM response JSON: {err}")

            # 4. Read usageMetadata and record via Budget.add_llm_usage
            usage = getattr(self, "last_usage_metadata", None) or {}
            prompt_tokens = int(usage.get("promptTokenCount", 0))
            candidates_tokens = int(usage.get("candidatesTokenCount", 0))
            thoughts_tokens = int(
                usage.get("thoughtsTokenCount", 0)
                or usage.get("candidatesTokensDetails", {}).get("thoughtsTokenCount", 0)
                or 0
            )
            output_tokens = candidates_tokens + thoughts_tokens

            answered_model = self.last_model_used or self.model_name
            pricing = self.pricing.get(answered_model, self.pricing.get("default", {"input": 0.075, "output": 0.30}))
            cost_usd = (prompt_tokens / 1_000_000.0 * pricing["input"]) + (output_tokens / 1_000_000.0 * pricing["output"])

            self.total_tokens_in += prompt_tokens
            self.total_tokens_out += output_tokens
            self.last_tokens_in = prompt_tokens
            self.last_tokens_out = output_tokens
            self.total_cost_usd += cost_usd

            if budget is not None:
                budget.add_llm_usage(
                    prompt_tokens=prompt_tokens,
                    candidates_tokens=candidates_tokens,
                    thoughts_tokens=thoughts_tokens,
                    cost_usd=round(cost_usd, 6),
                )

            # 5. Save to disk cache (only successful responses)
            cache_payload = {
                "cache_key": cache_key,
                "prompt_version": PROMPT_VERSION,
                "model": answered_model,
                "extracted_items": extracted_items,
                "prompt_tokens": prompt_tokens,
                "candidates_tokens": candidates_tokens,
                "thoughts_tokens": thoughts_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
            }
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(cache_payload, f, indent=2)
            except Exception:
                pass

        # 6. Verify quotes and create Fact objects
        facts: List[Fact] = []
        now = datetime.now(timezone.utc)
        model_for_fact = self.last_model_used or self.model_name

        for item in extracted_items:
            if not isinstance(item, dict):
                self.discarded_count += 1
                continue

            field = str(item.get("field", "")).strip().lower()
            value = item.get("value")
            quote = item.get("quote")

            # Must be in allowed list & not in forbidden/financial lists
            if field not in ALLOWED_LLM_FIELDS or field in FORBIDDEN_LLM_FIELDS:
                self.discarded_count += 1
                continue

            # Code verification via verify_quote
            if not verify_quote(quote, truncated_text, value):
                self.discarded_count += 1
                continue

            facts.append(
                Fact(
                    orgnr=orgnr,
                    field=field,
                    value=value,
                    source_name="LLM Extraction",
                    source_url=source_url,
                    retrieved_at=now,
                    method="llm_verified",
                    match_basis=match_basis,
                    evidence_quote=quote,
                    confidence=0.85,
                    metadata={
                        "model": model_for_fact,
                        "llm_model": model_for_fact,
                    },
                )
            )

        return facts


class FakeLLMClient(LLMClient):
    """Fake LLM client for unit tests returning configurable fake JSON responses with simulated retries and errors."""

    def __init__(
        self,
        fake_items: Optional[List[dict]] = None,
        responses: Optional[List[Any]] = None,
        fast_sleep: bool = True,
        **kwargs: Any,
    ) -> None:
        if "api_key" not in kwargs and not os.getenv("GEMINI_API_KEY"):
            kwargs["api_key"] = "fake-api-key"
        super().__init__(**kwargs)
        self.fake_items = fake_items if fake_items is not None else []
        self.responses = list(responses) if responses is not None else None
        self.fast_sleep = fast_sleep
        self.call_history: List[Dict[str, Any]] = []
        if not self.last_usage_metadata:
            self.last_usage_metadata = {
                "promptTokenCount": 150,
                "candidatesTokenCount": 50,
                "thoughtsTokenCount": 0,
            }

    async def _sleep(self, seconds: float) -> None:
        if not self.fast_sleep:
            await asyncio.sleep(seconds)

    def _make_success_response(self, model: str) -> httpx.Response:
        body = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": json.dumps(self.fake_items)}]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": self.last_usage_metadata or {
                "promptTokenCount": 150,
                "candidatesTokenCount": 50,
                "thoughtsTokenCount": 0,
            },
        }
        return httpx.Response(
            status_code=200,
            json=body,
            request=httpx.Request("POST", f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"),
        )

    async def _send_api_request(self, model: str, payload: dict) -> httpx.Response:
        self.call_history.append({"model": model, "payload": payload})
        if self.responses is not None and len(self.responses) > 0:
            resp_item = self.responses.pop(0)
            if isinstance(resp_item, tuple):
                status = resp_item[0]
                hdrs = resp_item[1] if len(resp_item) > 1 else {}
                txt = resp_item[2] if len(resp_item) > 2 else f"Status {status}"
                return httpx.Response(status_code=status, headers=hdrs, text=txt, request=httpx.Request("POST", f"https://fake/{model}"))
            elif isinstance(resp_item, int):
                if resp_item == 200:
                    return self._make_success_response(model)
                return httpx.Response(status_code=resp_item, text=f"HTTP {resp_item} error", request=httpx.Request("POST", f"https://fake/{model}"))
            elif isinstance(resp_item, httpx.Response):
                return resp_item
            elif isinstance(resp_item, Exception):
                raise resp_item

        return self._make_success_response(model)

