# Norway Profiler

`profiler` is a Python 3.12+ agentic Norwegian company profiling engine. It takes a Norwegian organisation number (9 digits) and returns a fully sourced, audited company profile.

## Non-Negotiable Core Rules

1. **Accuracy First**: Wrong-company facts and fabricated financial values disqualify the project. When unsure, DROP the fact. Never guess.
2. **Strict Provenance**: Every published fact requires `source_url`, `retrieved_at`, `source_date` (if known), `method`, and `match_basis`. Facts missing these are unpublishable by the type system.
3. **Financial Integrity**: Financial values come ONLY from Regnskapsregisteret API or parsed filed accounts. Never from an LLM, never estimated. If absent: `null` + reason `"no filed accounts found"`.
4. **Verbatim LLM Quotes**: LLM-extracted values MUST carry a quote that appears VERBATIM in fetched page text and contains the value.
5. **No Name-Only Matching**: Never match a company by name alone. Match MUST be based on verified 9-digit `orgnr`.
6. **Execution Budget Limits**: Max 100 companies, 45 minutes runtime, 2000 outbound requests, and $10 API cost per daily run.

## Tech Stack

- **Async HTTP**: `httpx` with `tenacity` retry logic
- **Data Validation**: `pydantic` (v2)
- **HTML Parsing**: `selectolax`
- **CLI**: `typer` and `rich`
- **Storage**: `sqlite3`
- **Testing**: `pytest` and `respx`

## LLM & Model API

- **Provider**: Google Gemini API
- **Model**: Configured strictly via `LLM_MODEL` environment variable (e.g. `LLM_MODEL=gemini-3.6-flash`).
  > **Note**: `LLM_MODEL` must be explicitly configured in `.env` or the shell environment. If `LLM_MODEL` is unset, the profiler fails fast with a clear error message instead of defaulting silently.
- **Authentication**: Configured via `GEMINI_API_KEY`.


## Installation

```bash
pip install -e .
```

## Usage

### CLI Profile Lookup
```bash
profiler profile 923609016 --json
```

### Batch Profile Lookup
```bash
profiler batch 923609016 986161474 --db norway_profiles.db
```

### Running Tests
```bash
pytest
```
