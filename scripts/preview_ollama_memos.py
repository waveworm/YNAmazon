"""Preview Ollama AI memo summarization using real cached order data.

Run with:
    uv run --extra ai python scripts/preview_ollama_memos.py

Reads item titles from .amazon_debug/orders/*.parse_debug.txt files and prints
the original titles alongside what Ollama would generate as a YNAB memo.
No YNAB changes are made.
"""

import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Inline the prompts so this script works standalone
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """
You are a helpful assistant that summarizes Amazon orders for YNAB memos.
Your goal is to create concise, readable summaries that fit within YNAB's 500 character limit.
Focus on preserving important information like order URLs and partial order warnings.

Rules:
- Omit all Amazon branding (don't include "Amazon", "Amazon Basics", "Amazon Essentials", etc.)
- Keep descriptions under 50 characters whenever possible
- Focus on the essential details of what the item is
- Remove brands when possible unless it's a notable brand
- Preserve the original casing of all words
- Omit quantity information when the quantity is 1 (don't include '(1)' in the description)
- If quantity is greater than 1, include it in parentheses like "(3)"
- Do not use sentence case or title case - keep the casing exactly as provided in the input
"""

PLAIN_PROMPT = """
Please provide a concise summary of this Amazon order.

For a single item order, just list the item name by itself with no prefix. If a quantity is mentioned and greater than 1, put it in parentheses at the end.
For example: "Barrel of Maple Syrup" or "Protein Bars (12)"

For multiple items, list them with a comma-separated format, with quantities in parentheses at the end of each item:
For example: "Dog Costume, Facepaint (2), Popcorn-shaped Purse"

Do not include the order URL or any additional information - just the item list.
"""


# ---------------------------------------------------------------------------
# Parse .parse_debug.txt files
# ---------------------------------------------------------------------------
def parse_debug_file(path: Path) -> dict:
    """Extract order_id, total, and paired items from a parse_debug file."""
    order_id = path.stem.replace("order_", "")
    total = None
    items = []

    for line in path.read_text().splitlines():
        m = re.match(r"^gift_total=([\d.]+)", line)
        if m:
            total = m.group(1)
        m = re.match(r"^\s+\*\s+(.+?)\s+->\s+([\d.]+)$", line)
        if m:
            items.append((m.group(1).strip(), m.group(2)))

    return {"order_id": order_id, "total": total, "items": items}


def load_orders(debug_dir: Path, limit: int = 6) -> list[dict]:
    """Load orders that have paired items, newest files first."""
    files = sorted(debug_dir.glob("order_*.parse_debug.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    orders = []
    for f in files:
        data = parse_debug_file(f)
        if data["items"]:
            orders.append(data)
        if len(orders) >= limit:
            break
    return orders


# ---------------------------------------------------------------------------
# Ollama call
# ---------------------------------------------------------------------------
def summarize(items: list[str], model: str, base_url: str) -> str:
    try:
        from openai import OpenAI, APIConnectionError
    except ImportError:
        return "[openai package not installed — run: uv sync --extra ai]"

    client = OpenAI(base_url=base_url, api_key="ollama")
    items_text = "\n".join(f"- {item}" for item in items)
    full_prompt = f"{PLAIN_PROMPT}\n\nOrder Details:\n{items_text}"

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": full_prompt},
            ],
        )
        return resp.choices[0].message.content or "(empty response)"
    except APIConnectionError:
        return f"[connection error — is Ollama running at {base_url}?]"
    except Exception as e:
        return f"[error: {e}]"


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
def hr(char="─", width=80):
    print(char * width)


def print_order(order: dict, ai_memo: str):
    hr()
    print(f"Order {order['order_id']}  (${order['total']})")
    print()
    print("  ORIGINAL TITLES:")
    for title, price in order["items"]:
        # Truncate long titles for display
        display = title if len(title) <= 90 else title[:87] + "..."
        print(f"    ${price:>7}  {display}")
    print()
    print("  AI MEMO:")
    for line in ai_memo.splitlines():
        print(f"    {line}")
    print(f"  [{len(ai_memo)} chars]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    model    = os.environ.get("OLLAMA_MODEL",    "llama3.2")

    debug_dir = Path(__file__).parent.parent / ".amazon_debug" / "orders"
    if not debug_dir.exists():
        print(f"Debug dir not found: {debug_dir}")
        sys.exit(1)

    orders = load_orders(debug_dir, limit=6)
    if not orders:
        print("No orders with paired items found in", debug_dir)
        sys.exit(1)

    print(f"Ollama preview  |  model={model}  base_url={base_url}")
    print(f"Testing {len(orders)} orders from .amazon_debug/orders/\n")

    for order in orders:
        item_titles = [title for title, _ in order["items"]]
        ai_memo = summarize(item_titles, model=model, base_url=base_url)
        print_order(order, ai_memo)

    hr()
    print("\nNo YNAB changes made. Review output above before enabling AI memos.")


if __name__ == "__main__":
    main()
