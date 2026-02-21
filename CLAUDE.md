# YNAmazon — Claude Code Reference

## Project Overview

YNAmazon enriches YNAB transactions with Amazon order details. It logs into Amazon via web scraping, fetches order history and transactions, matches them to YNAB transactions by dollar amount, generates detailed memos (optionally AI-summarized), and updates YNAB via its REST API.

## Running the Tool

```bash
# Standard run (uses .env settings)
uv run yna

# Run with options
uv run yna ynamazon --days 45 --years 2025

# Print YNAB or Amazon transactions only (no updates)
uv run yna print-ynab
uv run yna print-amazon

# Payment transactions mode (see README for setup)
uv run yna create-missing
```

## Running Tests

```bash
# Install test dependencies first
uv sync --group test

# Run all tests
uv run --group test pytest tests/ -v

# Run a single test file
uv run --group test pytest tests/ynab/test_utilities.py -v

# Run with coverage
uv run --group test pytest tests/ --cov=src/ynamazon
```

### Known Test Issues (Pre-existing)

1. **`ModuleNotFoundError: No module named 'openai'`** — `ynab_memo.py` has unconditional top-level imports from `openai`, but `openai` is declared as an optional extra (`uv sync --extra ai`). To run `test_memo_truncation.py`, install with extras: `uv sync --extra ai --group test`.

2. **`ValidationError: amazon_user — Field required`** — The settings object is instantiated at module import time (`settings = Settings()` in `settings.py`). Tests that import anything from `ynamazon` will fail if the `.env` file is missing `AMAZON_USER`. The `.env` file must use `AMAZON_USER=` (not `AMAZON_USERNAME=`). Check `.env-template` for the correct variable names.

## Environment Setup

Copy `.env-template` to `.env` and fill in values. Key variables:

| Variable | Required | Notes |
|----------|----------|-------|
| `YNAB_API_KEY` | Yes | YNAB personal access token |
| `YNAB_BUDGET_ID` | Yes | From YNAB URL |
| `YNAB_TARGET_ACCOUNT_ID` | Yes | Default account for transactions |
| `AMAZON_USER` | Yes | Amazon login email (note: NOT `AMAZON_USERNAME`) |
| `AMAZON_PASSWORD` | Yes | Amazon password |
| `OPENAI_API_KEY` | If AI enabled | Requires `uv sync --extra ai` too |
| `AMAZON_USE_PAYMENT_TRANSACTIONS` | Recommended | Enable payment-page scraping mode |
| `YNAMAZON_DEBUG` | Optional | Set `true` for safe testing (blue-flagged transactions) |

## Key Files

| File | Purpose |
|------|---------|
| `src/ynamazon/main.py` | Entry point for standard mode; interactive YNAB update flow |
| `src/ynamazon/amazon_transactions.py` | Amazon login + order/transaction fetching via `amazon-orders` library |
| `src/ynamazon/ynab_transactions.py` | YNAB API calls; payee lookup, transaction fetch, update |
| `src/ynamazon/ynab_memo.py` | Memo processing: AI summarization and/or truncation to 500 chars |
| `src/ynamazon/settings.py` | Pydantic settings loaded from `.env` at import time |
| `src/ynamazon/cli/cli.py` | Typer CLI definitions; entry point is `yna` command |
| `create_missing_ynamazon.py` | Large standalone script for Payment Transactions Mode (~2700 lines) |
| `tests/` | pytest test suite; uses `polyfactory` for Pydantic model factories |

## Architecture: How It Works

```
1. Load .env → Settings (Pydantic BaseSettings)
2. Fetch YNAB transactions (payee = "Amazon - Needs Memo", unapproved only)
3. Login to Amazon (Selenium-based, ~30-120s — dominant bottleneck)
4. Fetch Amazon order history + recent transactions
5. For each YNAB transaction:
   a. Find matching Amazon transaction by amount (Decimal comparison)
   b. Build memo (items list + order URL)
   c. Optionally AI-summarize (OpenAI gpt-4o-mini)
   d. Truncate to 500 chars if needed
   e. Confirm with user (interactive) → update YNAB via API
```

## Performance Notes

**The dominant bottleneck is Amazon web scraping.** The `amazon-orders` library uses Selenium (headless browser) to authenticate and scrape Amazon. This typically takes **30–120 seconds** and cannot be improved at the Python code level — it is constrained by Amazon's page load times and Selenium overhead.

### Other Performance Observations

- **`locate_amazon_transaction_by_amount`** does a linear scan (O(n)) per YNAB transaction. For typical dataset sizes (<50 transactions) this is negligible, but could be replaced with a dict for O(1) lookup if ever needed.
- **YNAB API calls** are fast REST calls; two are made at startup (get payees, get transactions).
- **OpenAI client** (`generate_ai_summary`) creates a new `OpenAI()` client on every invocation. For typical use (one run, few transactions) this is not a problem.
- **`translate_hybrid_to_temp`** uses `model_dump()` + `model_validate()` round-trip for type conversion — correct and safe but slightly heavier than necessary; acceptable for small datasets.

### If Runs Are Too Slow

- Reduce `--days` (transaction lookback window)
- Reduce `--history-pages` (pages of Amazon order history to scan)
- Use `YNAMAZON_DEBUG=true` for testing on a small date window (forces 10-day lookback)
- The Amazon login can be slow on first run; subsequent runs in the same session may be faster depending on browser cookie caching in `amazon-orders`

## Key Design Patterns

- **Pydantic everywhere**: Models for settings, Amazon data, YNAB data. Use `model_validate()` not direct construction for cross-model conversion.
- **Settings at import time**: `settings = Settings()` runs when any module is imported. Changes to `.env` require process restart. Tests must mock `settings` attributes directly.
- **YNAB API via context managers**: `with ApiClient(configuration=...) as api_client:` — always use this pattern for YNAB calls.
- **YNAB amounts in milliunits**: YNAB stores amounts as integers × 1000. `amount_decimal` property divides by 1000. Amazon amounts are positive Decimal; YNAB outflows are negative.
- **Amount matching uses Decimal**: `locate_amazon_transaction_by_amount` compares `a_tran.transaction_total == -amount` (note the inversion). Amazon `transaction_total` is inverted on ingestion via `field_validator`.
- **YNAB memo limit is 500 characters**: `process_memo()` in `ynab_memo.py` handles truncation and AI summarization. There is also a secondary safety truncation in `update_ynab_transaction()`.
- **Optional AI extra**: `openai` must be installed separately (`uv sync --extra ai`). However, `ynab_memo.py` currently imports `openai` unconditionally at module level, so tests for that module require the extra installed.

## Testing Strategy

- `tests/factories.py` — `polyfactory` model factories for YNAB Pydantic models
- `tests/ynab/test_utilities.py` — tests for `markdown_formatted_link/title` (uses `@patch` for settings)
- `tests/ynab/test_memo_truncation.py` — tests for memo processing, truncation, AI summarization (requires `openai` extra)
- `tests/amazon/test_transactions.py` — tests for Amazon order history fetching (uses `MagicMock` for Selenium session)

**When mocking settings**, patch attributes directly on the imported `settings` object rather than replacing the whole object:
```python
from ynamazon.settings import settings
settings.use_ai_summarization = True  # patch directly
```
Or use `@patch("ynamazon.module_name.settings")` to replace the module-level reference entirely.

## Lint / Type Check

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run mypy src/
```

## CLI Reference

See [CLI_README.md](CLI_README.md) for full auto-generated CLI documentation.

## Debug Mode

Set `YNAMAZON_DEBUG=true` to run safely:
- All created transactions get a **blue flag** (easy to filter and delete in YNAB)
- Forces 10-day lookback
- Uses timestamp-based import IDs (bypasses duplicate prevention)
- Safe to run repeatedly; delete blue-flagged transactions in YNAB when done
