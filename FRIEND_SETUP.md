# Friend Setup (Run YNAmazon on Another Machine)

This guide is for someone receiving a source bundle of this project.

## 1) Prerequisites

- macOS, Linux, or Windows (WSL recommended on Windows)
- Python 3.9 - 3.12
- [`uv`](https://docs.astral.sh/uv/) installed

Quick install examples:

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## 2) Unpack the project

```bash
tar -xzf YNAmazon-YYYYMMDD-HHMMSS.tar.gz
```

Then open a terminal in the extracted project directory.

## 3) Create your local config file

```bash
cp .env-template .env
```

Open `.env` and fill in at least:

- `YNAB_API_KEY`
- `YNAB_BUDGET_ID`
- `YNAB_TARGET_ACCOUNT_ID`
- `AMAZON_USER`
- `AMAZON_PASSWORD`

Optional:

- `OPENAI_API_KEY` (only if `USE_AI_SUMMARIZATION=true`)
- payment-routing vars like `YNAB_GIFT_CARD_ACCOUNT_ID`, `YNAB_DEFAULT_CREDIT_CARD_ACCOUNT_ID`

## 4) Install dependencies

```bash
# without AI summarization
uv sync

# with AI summarization support
uv sync --extra ai
```

## 5) Run YNAmazon

Main command:

```bash
uv run yna
```

Other useful commands:

```bash
uv run yna --help
uv run yna print-amazon
uv run yna print-ynab
uv run yna create-missing --help
```

## 6) Troubleshooting

- If login fails, verify Amazon credentials and complete any MFA/CAPTCHA in browser when prompted.
- If YNAB errors occur, re-check API key/budget/account IDs.
- If Python version errors appear, install Python 3.12 (or any 3.9-3.12) and rerun `uv sync`.
