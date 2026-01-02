#!/usr/bin/env python3
"""
YNAmazon – Create or update itemized Amazon orders in YNAB

Overview
  - Collect order IDs from Amazon Gift Card (GC) activity pages and/or order history.
  - Save each order's details HTML (idempotent: never clobbers existing files).
  - Parse each order for a Gift Card total and per‑item titles/prices (prefers structured
    data from amazonorders' API when available; otherwise uses a resilient HTML heuristic).
  - Build split transactions, scaling line amounts to the GC total when required, while
    preserving the exact receipt price in each split memo e.g. "(orig 12.34)".
  - Post to YNAB with create‑or‑update behavior controlled by environment guardrails so
    approved/categorized work is never overwritten unless you explicitly allow it.

Quick usage
  # Standard GC mode, reading env from .env
  uv run --env-file YNAmazon/.env python3 YNAmazon/create_missing_ynamazon.py

  # Also scan order-history pages 1–3 to pull older orders
  uv run --env-file YNAmazon/.env python3 YNAmazon/create_missing_ynamazon.py -p 3

Key environment variables (set in YNAmazon/.env)
  YNAB_API_KEY, YNAB_BUDGET_ID, YNAB_TARGET_ACCOUNT_ID: Required YNAB credentials.
  YNAB_GIFT_CARD_ACCOUNT_ID: Optional account ID for gift card purchases.
  YNAB_DEFAULT_CREDIT_CARD_ACCOUNT_ID: Optional default account for credit card purchases.
  YNAB_CREDIT_CARD_ACCOUNTS: Optional JSON mapping card last-4-digits to account IDs.
      Example: '{"1234": "account-uuid-for-citi", "5678": "account-uuid-for-chase"}'
  YNAB_UPDATE_EXISTING=true: Enable PATCHing by import_id (otherwise create‑only).
  YNAB_UPDATE_ONLY_UNAPPROVED=true: Only update unapproved transactions (default true).
  YNAB_UPDATE_ONLY_UNCATEGORIZED=true: Only update uncategorized transactions (default true).
  YNAB_UPDATE_ONLY_EMPTY_SPLITS=true: Only update transactions without existing splits (default true).
  YNAB_UPDATE_ONLY_PAYEE_NAME="Amazon - Needs Memo": Optional staging payee guard.
  YNAB_IMPORT_ID_TAG=...: Optional suffix for import_id to bypass YNAB's duplicate memory.
  YNAB_CREATE_DUMMY_TEST=true: Create a $1 dummy transaction and exit (connectivity test).

  AMAZON_USE_PAYMENT_TRANSACTIONS=true: Use the payment transactions page to get accurate
      payment method and amount for each order. This is the recommended mode for tracking
      both gift card and credit card purchases with proper itemized splits.
  AMAZON_GC_ACTIVITY=true: Enable GC activity scraping (find order IDs on GC pages).
  AMAZON_GC_PLAYWRIGHT=true: Use Playwright to drive login and save order details.
  AMAZON_DUMP_DIR=./.amazon_debug: Where HTML/debug artifacts are saved.
  AMAZON_PARSER_DEBUG=true: Write per‑order parse_debug files for tuning.
  AMAZON_EVEN_SPLIT_FALLBACK=false: If true, when prices can't be paired, evenly split the
    GC total across plausible product titles (capped to 8); otherwise a single summary line.

CLI flags
  -p, --history-pages N: Also scan N pages per year from order history (page 1 is the default page).
  --history-page-size N: Override the assumed page size (default 10) for startIndex math.

Design notes
  - Idempotent download and posting: HTML and YNAB import_ids are stable; we skip/reuse where possible.
  - Robustness: We prefer the amazonorders structured API for item prices and dates, with an
    HTML heuristic as fallback. All memos are truncated to 500 characters for YNAB.
  - Safety: Update guardrails default to protecting approved/categorized/with‑splits transactions.
"""
import os
import json
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import csv
import re
from pathlib import Path
from urllib.parse import urlencode
import html as _html_mod

# --- Helpers for extracting money and items from Amazon HTML (for GC/manual splits) ---
CURRENCY_RE = re.compile(r'[-+]?\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})|[0-9]+(?:\.[0-9]{2}))')

def _to_decimal_money(s: str) -> Decimal:
    """
    Convert a money-ish string to Decimal dollars (e.g. "-$21.84" -> Decimal('21.84') with sign).
    Returns Decimal('0') if nothing is found.
    """
    if not s:
        return Decimal("0")
    s = s.strip()
    sign = -1 if "-" in s and "(" not in s else 1
    m = CURRENCY_RE.search(s)
    if not m:
        return Decimal("0")
    val = Decimal(m.group(1).replace(",", ""))
    return (val * sign).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# Try to parse an order date from HTML (best-effort; GC HTML varies by region)
MONTHS = (
    "January","February","March","April","May","June","July","August","September","October","November","December"
)
MONTHS_RE = r"January|February|March|April|May|June|July|August|September|October|November|December"

def _parse_order_date_from_html(html: str) -> Optional[dt.date]:
    if not html:
        return None
    try:
        # Remove script/style blocks, strip tags, unescape entities, compress whitespace
        cleaned = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
        cleaned = re.sub(r"<style[\s\S]*?</style>", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = _html_mod.unescape(cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        # Search for label then Month Day, Year within a short window
        label_re = re.compile(rf"(order\s*(?:placed|date)|ordered\s*on)\b", re.IGNORECASE)
        for m in label_re.finditer(cleaned):
            window = cleaned[m.end(): m.end()+160]
            mdy = re.search(rf"\b({MONTHS_RE})\s+(\d{{1,2}}),\s*(\d{{4}})\b", window, flags=re.IGNORECASE)
            if mdy:
                month = mdy.group(1).title()
                day = int(mdy.group(2))
                year = int(mdy.group(3))
                return dt.datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date()
            iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", window)
            if iso:
                try:
                    return dt.date.fromisoformat(iso.group(1))
                except Exception:
                    pass
        # Fallback: first Month Day, Year on page
        any_mdy = re.search(rf"\b({MONTHS_RE})\s+(\d{{1,2}}),\s*(\d{{4}})\b", cleaned, flags=re.IGNORECASE)
        if any_mdy:
            month = any_mdy.group(1).title()
            day = int(any_mdy.group(2))
            year = int(any_mdy.group(3))
            return dt.datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date()
    except Exception:
        return None
    return None


# Parse an Amazon Order Details HTML page for gift card total and items
def parse_order_details_html(html: str) -> Tuple[Decimal, List[Tuple[str, Decimal]]]:
    """
    Parse an Amazon Order Details HTML page and try to return:
      - gift_card_total: absolute value of the gift card applied to the order (Decimal, positive)
      - items: list of (title, price Decimal) per item found on the page
    Heuristic parser with safeguards to avoid picking summary totals as item prices.
    """
    gift_total = Decimal("0.00")
    items: List[Tuple[str, Decimal]] = []

    if not html:
        return gift_total, items

    # 1) Gift Card Amount from the Order Summary box
    # Try multiple labels and allow a wider capture window to find the currency value after the label.
    def _find_amount_after(label_regex: str, window: int = 500) -> Decimal:
        m = re.search(label_regex, html, flags=re.IGNORECASE)
        if not m:
            return Decimal("0")
        snippet = html[m.start(): m.start() + window]
        return _to_decimal_money(snippet)

    for label in (
        r'Gift\s*Card\s*Amount',
        r'Gift\s*Certificate\s*/?\s*Card',
        r'Gift\s*card\s*balance',
    ):
        amt = _find_amount_after(label, window=600)
        if amt != 0:
            gift_total = abs(amt)
            break

    # Identify summary blocks to avoid (Order Summary, totals, shipping, taxes, gift card amount lines)
    forbidden_regions: List[Tuple[int, int]] = []
    for label in (
        r'Order\s*Summary', r'Order\s*Total', r'Total\s*before\s*tax', r'Subtotal', r'Shipping', r'Estimated\s*tax',
        r'Gift\s*Card\s*Amount', r'Gift\s*Certificate\s*/?\s*Card', r'Gift\s*card\s*balance',
    ):
        for m in re.finditer(label, html, flags=re.IGNORECASE):
            start = max(0, m.start() - 100)
            end = min(len(html), m.start() + 1200)
            forbidden_regions.append((start, end))

    def _in_forbidden(pos: int) -> bool:
        for a, b in forbidden_regions:
            if a <= pos <= b:
                return True
        return False

    # 2) Collect item title anchors that look like product links (strict) and exclude store/help/nav links.
    title_spans: List[Tuple[int, str]] = []
    for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{3,200})</a>', html, flags=re.IGNORECASE):
        href = (m.group(1) or "")
        hlow = href.lower()
        title = re.sub(r'\s+', ' ', m.group(2)).strip()
        tlow = title.lower()
        product_like = (
            ('/dp/' in hlow) or ('/gp/product' in hlow) or ('/gp/aw/d' in hlow) or ('/hz/product' in hlow) or
            bool(re.search(r'[?&]asin=[a-z0-9]{10}', hlow))
        )
        if not product_like:
            continue
        if any(bad in hlow for bad in ('/stores/', '/aag/', '/seller', '/gp/help', '/gp/video', '/gp/browse', '/gp/cart', '/wishlist', '/hz/wishlist')):
            continue
        banned = (
            'view invoice', 'track package', 'get product support', 'return or replace', 'write a product review',
            'share gift', 'subscribe', 'buy it again', 'order details', 'gift card', 'order id', 'order date',
            'shipped on', 'sold by', 'view your', 'view order', 'arriving', 'delivered', 'shipping', 'total',
            'main content', 'amazon haul', 'off to college', 'luxury', 'prime video', 'amazon basics',
            'keep shopping for', 'video games', 'household, health & baby care', 'customer service',
            'kindle books', 'pet supplies', 'buy again', 'same-day delivery', 'handmade', "today's deals",
            'amazon secured card', 'amazon business card', 'prime visa', 'prime mastercard', 'credit card',
            'business card', 'secured card', 'visa card', 'mastercard', 'discover card',
        )
        if any(x in tlow for x in banned):
            continue
        if len(title) < 4:
            continue
        title_spans.append((m.start(), title))

    # 3) Collect price candidates excluding summary/forbidden regions
    price_spans: List[Tuple[int, Decimal]] = []
    for m in re.finditer(r'(?:\$|\u00A3|\u20AC)\s*[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})', html):
        pos = m.start()
        if _in_forbidden(pos):
            continue
        price = _to_decimal_money(m.group(0))
        # Avoid capturing the overall gift card total as an item price
        if gift_total != 0 and abs(price - gift_total) <= Decimal('0.01'):
            continue
        price_spans.append((pos, price))

    # 4) Pair titles to prices using Amazon price markup first (a-offscreen / a-price-whole+fraction)
    def _find_price_near(pos: int) -> Optional[Tuple[int, Decimal]]:
        window = 1600
        chunk = html[pos: pos + window]
        m = re.search(r'class\s*=\s*"[^"]*a-offscreen[^"]*"[^>]*>\s*([^<]{1,20})<', chunk, flags=re.IGNORECASE)
        if m:
            val = _to_decimal_money(m.group(1))
            if val != 0:
                return (pos + m.start(1), val)
        m2w = re.search(r'class\s*=\s*"[^"]*a-price-whole[^"]*"[^>]*>\s*([0-9,]{1,7})\s*<', chunk, flags=re.IGNORECASE)
        m2f = re.search(r'class\s*=\s*"[^"]*a-price-fraction[^"]*"[^>]*>\s*([0-9]{2})\s*<', chunk, flags=re.IGNORECASE)
        if m2w and m2f:
            try:
                val = Decimal(m2w.group(1).replace(',', '') + '.' + m2f.group(1))
                return (pos + m2w.start(1), val)
            except Exception:
                pass
        m3 = re.search(r'(?:\$|\u00A3|\u20AC)\s*[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})', chunk)
        if m3:
            return (pos + m3.start(), _to_decimal_money(m3.group(0)))
        return None

    used_price_idx: set[int] = set()
    for pos, title in title_spans:
        near = _find_price_near(pos)
        if near is not None:
            items.append((title, near[1]))
            continue
        # Fallback to global price list proximity
        best_idx = None
        best_dist = 10_000_000
        for pi, (ppos, price) in enumerate(price_spans):
            if pi in used_price_idx:
                continue
            if ppos < pos:
                continue
            dist = ppos - pos
            if dist < best_dist and dist < 2000:
                best_dist = dist
                best_idx = pi
        if best_idx is None:
            for pi, (ppos, price) in enumerate(price_spans):
                if pi in used_price_idx:
                    continue
                dist = abs(ppos - pos)
                if dist < best_dist and dist < 800:
                    best_dist = dist
                    best_idx = pi
        if best_idx is not None:
            used_price_idx.add(best_idx)
            items.append((title, price_spans[best_idx][1]))

    # De-dup obvious duplicate titles that sometimes appear twice (e.g., in sidebar)
    dedup: Dict[str, Decimal] = {}
    for title, price in items:
        dedup[title] = dedup.get(title, Decimal("0.00")) + price
    items = [(t, p.quantize(Decimal("0.01"))) for t, p in dedup.items()]

    # Optional fallback: If we found titles but failed to pair any prices, evenly split the gift total across titles.
    if not items and PARSER_EVEN_SPLIT_FALLBACK and gift_total > 0 and title_spans:
        seen_titles: List[str] = []
        for _, t in title_spans:
            if t not in seen_titles:
                seen_titles.append(t)
        n = min(len(seen_titles), 20)
        if n > 0:
            each = (gift_total / Decimal(n)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            amounts = [each] * n
            drift = gift_total - sum(amounts)
            amounts[-1] = (amounts[-1] + drift).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            items = [(seen_titles[i], amounts[i]) for i in range(n)]
            if PARSER_DEBUG:
                print(f"[parse-fallback] Even-split {gift_total} across {n} titles")

    return gift_total, items


def _renormalize_items_to_total(items: List[Tuple[str, Decimal]], total: Decimal) -> List[Tuple[str, Decimal]]:
    """
    Scale or redistribute item prices so that the sum equals `total`.
    If item sum is zero or we have no items, return a single synthetic line.
    """
    total = total.quantize(Decimal("0.01"))
    if not items:
        return [("Amazon order (gift card)", total)]
    sum_items = sum((p for _, p in items), Decimal("0.00")).quantize(Decimal("0.01"))
    if sum_items == 0:
        each = (total / Decimal(len(items))).quantize(Decimal("0.01"))
        return [(t, each) for t, _ in items]
    # scale amounts
    factor = (total / sum_items)
    scaled = [(t, (p * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) for t, p in items]
    # fix rounding drift on last line
    drift = total - sum((p for _, p in scaled), Decimal("0.00"))
    if scaled:
        t, p = scaled[-1]
        scaled[-1] = (t, (p + drift).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return scaled


# Helper: Truncate memo fields to YNAB's max length with ellipsis if needed
def _truncate_memo(text: str, limit: int = 500) -> str:
    """
    YNAB memo fields have a hard max length (error shows 500 chars).
    Trim anything longer and add an ellipsis so the API accepts it.
    """
    if not text:
        return text
    if len(text) <= limit:
        return text
    # Keep a few chars for the ellipsis
    return (text[: max(0, limit - 3)] + "...")

# Optional: Playwright + TOTP for interactive scraping
try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None
try:
    import pyotp
except Exception:
    pyotp = None

# Helper to get HTML text from amazonorders' response wrapper
def _resp_text(resp) -> str:
    try:
        base = getattr(resp, "response", None)
        if base is not None and getattr(base, "text", None) is not None:
            return base.text or ""
    except Exception:
        pass
    try:
        parsed = getattr(resp, "parsed", None)
        if parsed is not None:
            return str(parsed)
    except Exception:
        pass
    try:
        return getattr(resp, "text", "") or ""
    except Exception:
        return ""


# Helper to compute TOTP codes if pyotp and a secret are available
def _maybe_totp(secret: Optional[str]) -> Optional[str]:
    if not secret or not pyotp:
        return None
    try:
        return pyotp.TOTP(secret.strip().replace(" ", "")).now()
    except Exception:
        return None

from dotenv import load_dotenv

# ---- YNAB API (official Python client) ----
import ynab
import requests

# ---- Assumes you already have a way to fetch Amazon orders ----
# If you're using WoosterTech/YNAmazon internals, replace this stub with the project's
# real order loader. For demonstration, this expects a function `load_amazon_orders()`
# returning a list of dicts with:
# {
#   "order_id": "112-1234567-1234567",
#   "order_date": date,
#   "shipments": [
#       {"ship_date": date, "items": [{"title": "Widget", "qty": 1, "unit_price": 12.34}]}
#   ],
#   "payment_method": "Gift Card" or "Visa ..." (optional),
# }
#
# You can wire this to your existing amazon-orders download/cache.
from amazonorders.session import AmazonSession
from amazonorders.orders import AmazonOrders, AmazonOrdersError
from amazonorders.transactions import AmazonTransactions

# --- Monkey patch: allow orders with missing grand_total (e.g., fully gift-card-paid) ---
try:
    import types
    import amazonorders.entity.order as _ao_order_mod
    _orig_parse_gt = _ao_order_mod.Order._parse_grand_total

    def _parse_grand_total(self, **kwargs):  # NOTE: function name starts with `_parse_` on purpose
        try:
            return _orig_parse_gt(self, **kwargs)
        except Exception:
            # If Amazon changed the HTML or total is effectively $0 (gift card),
            # allow the object to be created. We'll compute totals from items later.
            return 0.0

    # Bind the function to the class so it looks like a proper method
    _ao_order_mod.Order._parse_grand_total = types.FunctionType(
        _parse_grand_total.__code__,
        globals(),
        name="_parse_grand_total",
        argdefs=_parse_grand_total.__defaults__,
        closure=_parse_grand_total.__closure__,
    )
    print("[amazon-orders] Enabled lenient grand_total parsing (gift-card friendly).")
except Exception as _mp_e:
    print(f"[amazon-orders] Could not apply lenient grand_total patch: {_mp_e}")


def load_amazon_orders(lookback_days: int) -> List[dict]:
    """
    Primary: shallow history + per-order details (for split lines).
    Fallback: transactions feed (one-line amounts) if order parsing fails
              (e.g., missing grand_total due to Amazon HTML changes).
    Returns normalized orders suitable for YNAB posting.
    """

    # Sanitize env values (strip quotes/whitespace)
    def _clean(v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip().strip('"').strip("'")

    session = AmazonSession(
        _clean(os.getenv("AMAZON_USERNAME")),
        _clean(os.getenv("AMAZON_PASSWORD")),
        otp_secret_key=_clean(os.getenv("AMAZON_OTP_SECRET_KEY")),
        debug=bool(os.getenv("AMZN_DEBUG"))
    )
    session.login()

    # --- Optionally dump Amazon order history HTML after login ---
    if os.getenv("AMAZON_DUMP_HISTORY_HTML", "").lower() in ("1", "true", "yes"):
        try:
            dump_dir = Path(os.getenv("AMAZON_DUMP_DIR") or ".amazon_debug").expanduser()
            dump_dir.mkdir(parents=True, exist_ok=True)
            today = dt.date.today()
            start_date = today - dt.timedelta(days=lookback_days)
            years = sorted({start_date.year, today.year})
            base = "https://www.amazon.com/gp/css/order-history"
            for y in years:
                url = f"{base}?{urlencode({'year': y})}"
                resp = session.get(url)
                html = _resp_text(resp)
                out = dump_dir / f"order_history_{y}.html"
                out.write_text(html, encoding="utf-8")
                print(f"[dump] Saved order history HTML for {y} → {out}")
        except Exception as e:
            print(f"[dump] Could not save order history HTML: {e}")

    today = dt.date.today()
    start_date = today - dt.timedelta(days=lookback_days)

    orders_api = AmazonOrders(session)

    inspect = (os.getenv("AMAZON_INSPECT", "").lower() in ("1", "true", "yes"))

    def _dump_order_debug(raw_order: Any, normalized_order: Optional[dict] = None):
        if not inspect:
            return
        print("\n--- AMAZON ORDER (raw) ---")
        try:
            oid = getattr(raw_order, "order_number", None) or getattr(raw_order, "id", None)
            print(f"order_id: {oid}")
            print(f"order_date: {getattr(raw_order, 'order_date', None)}")
            # Common fields to probe; not all will exist
            candidate_fields = [
                "grand_total", "payment_method", "total_before_tax", "item_subtotal",
                "shipping_handling", "shipping_total", "discounts_total", "subscribe_and_save_discount",
                "estimated_tax", "tax_total", "gift_card_amount", "promotions_total",
            ]
            for name in candidate_fields:
                val = getattr(raw_order, name, None)
                if val not in (None, ""):
                    print(f"{name}: {val}")
            # Items
            shipments = getattr(raw_order, "shipments", []) or []
            n_items = sum(len(getattr(s, "items", []) or []) for s in shipments)
            print(f"shipments: {len(shipments)} | items: {n_items}")
            for si, s in enumerate(shipments, 1):
                sd = getattr(s, "ship_date", None)
                print(f"  shipment[{si}] ship_date={sd}")
                for ii, it in enumerate(getattr(s, "items", []) or [], 1):
                    title = getattr(it, "title", None)
                    qty = getattr(it, "quantity", None)
                    price = getattr(it, "price", None)
                    if price is None:
                        price = getattr(it, "item_price", None)
                    print(f"    item[{ii}] {title!r} qty={qty} unit_price={price}")
        except Exception as e:
            print(f"[inspect] error dumping raw order: {e}")
        if normalized_order is not None:
            print("--- NORMALIZED ORDER ---")
            try:
                print(json.dumps(normalized_order, default=str, indent=2))
            except Exception as e:
                print(f"[inspect] error dumping normalized: {e}")
            print("------------------------\n")

    try:
        normalized: List[dict] = []
        skipped: List[str] = []
        years = sorted({start_date.year, today.year})

        for year in years:
            history = orders_api.get_order_history(year=year, full_details=False)
            for shallow in history:
                order_id = getattr(shallow, "order_number", None) or getattr(shallow, "id", None)
                order_date = getattr(shallow, "order_date", None)
                if not order_id or not order_date or order_date < start_date:
                    continue
                try:
                    o = orders_api.get_order(order_id)
                except Exception as e:
                    print(f"Skipping order {order_id}: {e}")
                    skipped.append(str(order_id))
                    continue

                if inspect:
                    _dump_order_debug(o)

                shipments = []
                for s in getattr(o, "shipments", []) or []:
                    ship_date = getattr(s, "ship_date", order_date)
                    items = []
                    for it in getattr(s, "items", []) or []:
                        title = getattr(it, "title", "") or ""
                        qty = int(getattr(it, "quantity", 1) or 1)
                        price = getattr(it, "price", None)
                        if price is None:
                            price = getattr(it, "item_price", 0)
                        items.append({
                            "title": title,
                            "qty": qty,
                            "unit_price": str(price or 0),
                        })
                    shipments.append({
                        "ship_date": ship_date,
                        "items": items,
                    })

                normalized.append({
                    "order_id": order_id,
                    "order_date": order_date,
                    "shipments": shipments,
                    "payment_method": getattr(o, "payment_method", None),
                })
                if inspect:
                    _dump_order_debug(o, normalized[-1])

        if skipped:
            preview = ", ".join(skipped[:6]) + (" ..." if len(skipped) > 6 else "")
            print(f"Note: {len(skipped)} order(s) were skipped (unsupported/HTML change): {preview}")

        print(f"[source] Website orders scraper → {len(normalized)} orders (normalized)")
        return normalized

    except AmazonOrdersError as e:
        print(f"Order history parsing failed, falling back to Transactions API: {e}")
    except Exception as e:
        # Any unexpected failure in the order path -> fallback as well
        print(f"Order path failed unexpectedly, falling back to Transactions API: {e}")

    # ---- Fallback: Use the transactions feed (one-line amounts) ----
    tx_api = AmazonTransactions(session)
    tx_list = tx_api.get_transactions(days=lookback_days)

    normalized_tx: List[dict] = []
    for tx in tx_list:
        tx_date = getattr(tx, "date", None)
        if not tx_date or tx_date < start_date:
            continue
        # Amount sign: assume charges are positive -> convert to outflow
        amt = getattr(tx, "amount", 0) or 0
        # Some storefronts report negatives for refunds; keep sign as-is and let YNAB handle
        # order_id may be present as `order_number` or embedded in description
        order_id = getattr(tx, "order_number", None) or getattr(tx, "order_id", None)
        desc = getattr(tx, "description", "Amazon Transaction") or "Amazon Transaction"

        # Synthesize a single-line shipment/item; we’ll use the tx amount as unit price
        shipments = [{
            "ship_date": tx_date,
            "items": [{
                "title": desc,
                "qty": 1,
                "unit_price": str(amt),
            }],
        }]

        normalized_tx.append({
            "order_id": order_id or f"TX-{tx_date.isoformat()}-{abs(int(amt*100))}",
            "order_date": tx_date,
            "shipments": shipments,
            "payment_method": getattr(tx, "payment_method", None) or "Amazon Transaction",
        })

    return normalized_tx


GC_ACTIVITY_URLS = [
    # Gift-card balance and activity pages — Amazon routes vary, so try these:
    "https://www.amazon.com/gc/balance",
    "https://www.amazon.com/hz/youraccount/gc/balance",  # alt route
]


def parse_amazon_gc_balance(html: str) -> Optional[Decimal]:
    """Parse Amazon gift card balance from the GC activity page HTML.
    
    Returns the balance as a Decimal, or None if not found.
    """
    # Look for the balance value in the gc-ui-balance element
    match = re.search(r'gc-ui-balance-gc-balance-value[^>]*>([^<]+)', html)
    if match:
        balance_text = match.group(1).strip()
        # Extract dollar amount (e.g., "$158.98" -> "158.98")
        amount_match = re.search(r'\$?([\d,]+\.?\d*)', balance_text)
        if amount_match:
            amount_str = amount_match.group(1).replace(',', '')
            try:
                return Decimal(amount_str)
            except Exception:
                pass
    return None


def get_amazon_gc_balance_from_file(gc_html_path: Path) -> Optional[Decimal]:
    """Read and parse Amazon GC balance from a saved HTML file."""
    if not gc_html_path.exists():
        return None
    try:
        html = gc_html_path.read_text(encoding="utf-8")
        return parse_amazon_gc_balance(html)
    except Exception:
        return None

ORDER_ID_RE = re.compile(r"(\d{3}[\-‑–—]\d{7}[\-‑–—]\d{7})")

# URL for Amazon's payment transactions page - shows all payments with method and amount
PAYMENTS_TRANSACTIONS_URL = "https://www.amazon.com/cpe/yourpayments/transactions"

def fetch_payment_transactions(session: AmazonSession, dump_dir: Path) -> List[Dict[str, Any]]:
    """Fetch and parse Amazon's payment transactions page.
    
    This page lists all payment transactions with:
    - Payment method (e.g., "Mastercard ****9669", "Amazon Gift Card")
    - Amount charged to that payment method
    - Order ID
    
    This is the authoritative source for split payments - if an order was paid
    with both gift card and credit card, it will appear twice with different
    payment methods and amounts.
    
    Returns: List of dicts with keys: order_id, payment_method, amount, date
    """
    dump_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        resp = session.get(PAYMENTS_TRANSACTIONS_URL)
        html = resp.response.text
        out = dump_dir / "transactions.html"
        out.write_text(html, encoding="utf-8")
        print(f"[payments] Saved transactions page → {out}")
    except Exception as e:
        print(f"[payments] Error fetching transactions page: {e}")
        return []
    
    return parse_payment_transactions_html(html)


def parse_payment_transactions_html(html: str) -> List[Dict[str, Any]]:
    """Parse the payment transactions HTML to extract order/payment/amount data.
    
    Returns: List of dicts with keys: order_id, payment_method, amount
    """
    transactions = []
    
    # Pattern for order IDs (normalize dashes)
    order_pattern = r'Order #(\d{3}-\d{7}-\d{7})'
    # Pattern for amounts (negative = charge)
    amount_pattern = r'-\$([0-9,]+\.[0-9]{2})'
    # Pattern for payment methods
    payment_pattern = r'(Mastercard \*{4}\d{4}|Visa \*{4}\d{4}|Amex \*{4}\d{4}|Discover \*{4}\d{4}|Amazon Gift Card)'
    
    # Find all matches with positions
    orders = [(m.start(), m.group(1)) for m in re.finditer(order_pattern, html)]
    amounts = [(m.start(), Decimal(m.group(1).replace(',', ''))) for m in re.finditer(amount_pattern, html)]
    payments = [(m.start(), m.group(1)) for m in re.finditer(payment_pattern, html)]
    
    # Build transactions by finding payment + amount pairs that precede order IDs
    # The HTML structure has: payment method, then amount, then order link
    for order_pos, order_id in orders:
        # Find the closest payment method and amount BEFORE this order
        closest_payment = None
        closest_amount = None
        
        for pay_pos, pay_method in reversed(payments):
            if pay_pos < order_pos:
                closest_payment = (pay_pos, pay_method)
                break
        
        for amt_pos, amt in reversed(amounts):
            if amt_pos < order_pos:
                closest_amount = (amt_pos, amt)
                break
        
        if closest_payment and closest_amount:
            # Check they're reasonably close to each other (within 2000 chars)
            if abs(closest_payment[0] - closest_amount[0]) < 2000:
                transactions.append({
                    'order_id': order_id,
                    'payment_method': closest_payment[1],
                    'amount': closest_amount[1]
                })
    
    print(f"[payments] Parsed {len(transactions)} payment transactions")
    return transactions


def load_orders_from_payment_transactions(lookback_days: int,
                                          skip_import_ids: Optional[set[str]] = None) -> List[dict]:
    """Load orders using the payment transactions page as the source of truth.
    
    This is the recommended mode for tracking both gift card and credit card purchases.
    It uses the payment transactions page to get the exact payment method and amount
    for each transaction, then fetches order details to get itemized splits.
    
    For split-payment orders (paid with both gift card and credit card), this creates
    separate normalized orders for each payment portion, with items scaled proportionally.
    
    Returns: List of normalized order dicts ready for YNAB posting.
    """
    def _clean(v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip().strip('"').strip("'")
    
    session = AmazonSession(
        _clean(os.getenv("AMAZON_USERNAME")),
        _clean(os.getenv("AMAZON_PASSWORD")),
        otp_secret_key=_clean(os.getenv("AMAZON_OTP_SECRET_KEY")),
        debug=bool(os.getenv("AMZN_DEBUG")),
    )
    session.login()
    
    dump_base = Path(os.getenv("AMAZON_DUMP_DIR") or ".amazon_debug").expanduser()
    payments_dir = dump_base / "payments"
    orders_dir = dump_base / "orders"
    orders_api = AmazonOrders(session)
    
    # Step 1: Fetch payment transactions
    payment_txns = fetch_payment_transactions(session, payments_dir)
    if not payment_txns:
        print("[payments] No payment transactions found.")
        return []
    
    # Step 2: Group by order_id to identify split payments
    from collections import defaultdict
    by_order: Dict[str, List[Dict]] = defaultdict(list)
    for txn in payment_txns:
        by_order[txn['order_id']].append(txn)
    
    print(f"[payments] Found {len(by_order)} unique orders from {len(payment_txns)} payment transactions")
    
    # Step 3: Fetch order details for each unique order
    unique_order_ids = list(by_order.keys())
    gc_fetch_order_details(session, unique_order_ids, orders_dir)
    
    # Step 4: Parse order details and build normalized orders for each payment
    today = dt.date.today()
    normalized_orders: List[dict] = []
    
    for order_id, payments in by_order.items():
        # Load and parse order details HTML
        order_html_path = orders_dir / f"order_{order_id}.html"
        try:
            html = order_html_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            html = ""
        
        # Parse items from HTML
        gift_total, items = parse_order_details_html(html)
        order_date = _parse_order_date_from_html(html) or today
        
        # Try to get better item data from API
        ship_date = None
        try:
            o = orders_api.get_order(order_id)
            if hasattr(o, "order_date") and o.order_date:
                order_date = o.order_date
            api_items: List[Tuple[str, Decimal]] = []
            for s in getattr(o, "shipments", []) or []:
                sd = getattr(s, "ship_date", None)
                if sd and (ship_date is None or sd < ship_date):
                    ship_date = sd
                for it in getattr(s, "items", []) or []:
                    title = getattr(it, "title", "") or "Item"
                    price = getattr(it, "price", None)
                    if price is None:
                        price = getattr(it, "item_price", 0)
                    try:
                        dprice = Decimal(str(price or 0))
                    except Exception:
                        dprice = Decimal("0")
                    if dprice != 0:
                        api_items.append((title, dprice))
            if api_items:
                items = api_items
        except Exception as e:
            if PARSER_DEBUG:
                print(f"[payments] API item fetch failed for {order_id}: {e}")
        
        if not ship_date:
            ship_date = order_date
        
        # Calculate total order value from items
        items_total = sum(p for _, p in items) if items else Decimal("0")
        
        # For each payment on this order, create a normalized order
        # with items scaled to match the payment amount
        total_payments = sum(p['amount'] for p in payments)
        
        for pay_idx, payment in enumerate(payments):
            pay_amount = payment['amount']
            pay_method = payment['payment_method']
            
            # Calculate proportion of this payment to total
            if total_payments > 0:
                proportion = pay_amount / total_payments
            else:
                proportion = Decimal("1") / Decimal(len(payments))
            
            # Scale items to match this payment amount
            if items and items_total > 0:
                # Scale each item proportionally
                scaled_items = []
                running_total = Decimal("0")
                for i, (title, price) in enumerate(items):
                    if i == len(items) - 1:
                        # Last item gets remainder to ensure exact total
                        scaled_price = pay_amount - running_total
                    else:
                        scaled_price = (price * proportion).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    running_total += scaled_price
                    scaled_items.append((title, scaled_price))
            else:
                # No items parsed, create single line
                scaled_items = [(f"Amazon order {order_id}", pay_amount)]
            
            # Build shipment items
            ship_items = []
            for title, scaled_price in scaled_items:
                ship_items.append({
                    "title": title,
                    "qty": 1,
                    "unit_price": str(scaled_price),
                })
            
            # Build import_id suffix for split payments
            import_id_suffix = f":p{pay_idx}" if len(payments) > 1 else ""
            
            normalized_orders.append({
                "order_id": order_id + import_id_suffix,  # Unique ID for each payment
                "order_id_base": order_id,  # Original order ID for memo
                "order_date": order_date,
                "shipments": [{
                    "ship_date": ship_date,
                    "items": ship_items,
                }],
                "payment_method": pay_method,
                "payment_amount": pay_amount,  # Exact amount from payment transactions
            })
    
    print(f"[source] Payment transactions → {len(normalized_orders)} orders with itemized splits")
    return normalized_orders


def collect_order_ids_from_history_pages(session: AmazonSession,
                                         start_date: dt.date,
                                         dump_dir: Path,
                                         pages: Optional[int] = None,
                                         page_size: Optional[int] = None) -> List[str]:
    """Collect additional order IDs from paginated order history.

    The standard GC activity pages often list recent orders; this helper augments the
    search by hitting order history pages with explicit pagination so you can pull older
    orders on page 2/3/etc. HTML is saved to `dump_dir` for inspection.

    Args:
      session: Authenticated AmazonSession
      start_date: Start of lookback window; used to select relevant years
      dump_dir: Where to write `order_history_<year>_p<page>.html`
      pages: Optional number of pages per year to fetch (if None, reads env)
      page_size: Optional assumed orders per page (defaults to 10 if None)

    Returns: List of order ID strings found across history pages.
    """
    """Optionally crawl Amazon's order history pages by year and startIndex to gather more Order IDs.
    Controlled by env:
      - AMAZON_ORDER_HISTORY_PAGES (int, default 1) → how many pages per year (page 1 is the default page)
      - AMAZON_ORDER_HISTORY_PAGE_SIZE (int, default 10) → assumed orders per page for startIndex math
    Saves raw HTML in dump_dir and returns any found order IDs.
    """
    if pages is None:
        pages = int(os.getenv("AMAZON_ORDER_HISTORY_PAGES", "1") or "1")
    if pages <= 1:
        return []
    if page_size is None:
        page_size = int(os.getenv("AMAZON_ORDER_HISTORY_PAGE_SIZE", "10") or "10")

    dump_dir.mkdir(parents=True, exist_ok=True)
    today = dt.date.today()
    years = sorted({start_date.year, today.year})
    base = "https://www.amazon.com/gp/css/order-history"
    found: List[str] = []

    for y in years:
        for pi in range(pages):
            start_idx = pi * page_size
            url = f"{base}?year={y}&startIndex={start_idx}"
            try:
                resp = session.get(url)
                html = _resp_text(resp)
                out = dump_dir / f"order_history_{y}_p{pi+1}.html"
                out.write_text(html, encoding="utf-8")
                print(f"[history] Saved order history {y} page {pi+1} → {out}")
                ids = set(ORDER_ID_RE.findall(html))
                if ids:
                    print(f"[history] Found {len(ids)} order IDs on history {y} p{pi+1}.")
                    found.extend(sorted(ids))
            except Exception as e:
                print(f"[history] Error fetching history y={y} p={pi+1}: {e}")
    return found
PARSER_DEBUG = os.getenv("AMAZON_PARSER_DEBUG", "").lower() in ("1","true","yes")
PARSER_EVEN_SPLIT_FALLBACK = os.getenv("AMAZON_EVEN_SPLIT_FALLBACK", "").lower() in ("1","true","yes")


def _dump_parse_debug(out_path: Path, html: str, gift_total: Decimal, items: List[Tuple[str, Decimal]]) -> None:
    try:
        lines = []
        lines.append(f"gift_total={gift_total}")
        # Titles via product-like anchors
        title_matches = list(re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{3,200})</a>', html, flags=re.IGNORECASE))
        product_titles = []
        for m in title_matches:
            href = (m.group(1) or '').lower()
            title = re.sub(r'\s+', ' ', m.group(2)).strip()
            if any(p in href for p in ('/dp/','/gp/product','/hz/product','/gp/aw/d')):
                product_titles.append(title)
        lines.append(f"candidate_product_titles={len(product_titles)}")
        for t in product_titles[:30]:
            lines.append(f"  - {t}")
        # Price matches excluding obvious summaries is complex; just count raw for context
        raw_prices = [m.group(0) for m in re.finditer(r'(?:\$|\u00A3|\u20AC)\s*[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})', html)]
        lines.append(f"raw_price_tokens={len(raw_prices)}")
        for p in raw_prices[:40]:
            lines.append(f"  $ {p}")
        lines.append(f"paired_items={len(items)}")
        for t,p in items:
            lines.append(f"  * {t} -> {p}")
        out_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"[parse-debug] Wrote {out_path}")
    except Exception as e:
        print(f"[parse-debug] Failed to write debug: {e}")


def load_orders_from_gc_playwright(lookback_days: int,
                                   skip_import_ids: Optional[set[str]] = None) -> List[dict]:
    """Interactive GC loader using Playwright.

    Signs in (optionally using TOTP), saves GC activity and per‑order details HTML, and
    parses orders similarly to the non‑Playwright path. Dates are derived from HTML when
    available. Use this when Amazon’s static pages don’t expose the needed content.
    """
    if sync_playwright is None:
        print("[gc-pw] Playwright is not installed. Run: uv run --with playwright --with pyotp python -c 'import playwright; print(\"ok\")' and then 'uv run playwright install' ")
        return []
    # env
    def _clean(v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip().strip('"').strip("'")
    username = _clean(os.getenv("AMAZON_USERNAME"))
    password = _clean(os.getenv("AMAZON_PASSWORD"))
    otp_secret = _clean(os.getenv("AMAZON_OTP_SECRET_KEY"))
    headless = os.getenv("AMAZON_PW_HEADLESS", "true").lower() in ("1","true","yes")
    dump_base = Path(os.getenv("AMAZON_DUMP_DIR") or ".amazon_debug").expanduser()
    gc_dir = dump_base / "gc_pw"
    orders_dir = dump_base / "orders_pw"
    gc_dir.mkdir(parents=True, exist_ok=True)
    orders_dir.mkdir(parents=True, exist_ok=True)

    if not username or not password:
        print("[gc-pw] Missing AMAZON_USERNAME or AMAZON_PASSWORD.")
        return []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        # Sign-in flow
        page.goto("https://www.amazon.com/ap/signin", wait_until="domcontentloaded")
        try:
            if page.locator("#ap_email").count() > 0:
                page.fill("#ap_email", username)
                page.click("#continue")
            if page.locator("#ap_password").count() > 0:
                page.fill("#ap_password", password)
                page.click("#signInSubmit")
            # MFA
            if page.locator("#auth-mfa-otpcode").count() > 0 and otp_secret:
                code = _maybe_totp(otp_secret)
                if code:
                    page.fill("#auth-mfa-otpcode", code)
                    # Some pages require clicking a different submit
                    if page.locator("#auth-signin-button").count():
                        page.click("#auth-signin-button")
                    elif page.locator("input[type=submit]").count():
                        page.locator("input[type=submit]").first.click()
            page.wait_for_load_state("networkidle")
        except Exception as e:
            print(f"[gc-pw] Sign-in issue: {e}")
        # Visit GC activity
        try:
            page.goto("https://www.amazon.com/gc/balance", wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            html = page.content()
            out = gc_dir / "gc_activity_1.html"
            out.write_text(html, encoding="utf-8")
            print(f"[gc-pw] Saved GC activity → {out}")
        except Exception as e:
            print(f"[gc-pw] Could not load GC page: {e}")
            browser.close()
            return []
        # Extract order ids
        ids = sorted(set(ORDER_ID_RE.findall(html)))
        if not ids:
            print("[gc-pw] No order IDs found on GC page.")
            browser.close()
            return []
        # Filter out already-posted import_ids unless updating
        upd = os.getenv("YNAB_UPDATE_EXISTING", "").lower() in ("1","true","yes")
        tag = (os.getenv("YNAB_IMPORT_ID_TAG") or "").strip().strip('"').strip("'")
        if skip_import_ids and not upd:
            filtered: List[str] = []
            for oid in ids:
                imp = f"YNAMAZON:{oid}" + (f":{tag}" if tag else "")
                if imp in skip_import_ids:
                    print(f"[gc-pw] Skipping already-posted import_id for {oid} ({imp})")
                    continue
                filtered.append(oid)
            ids = filtered
        print(f"[gc-pw] Found {len(ids)} order IDs on GC page.")
        # Fetch each order details page and save
        base = "https://www.amazon.com/gp/your-account/order-details?orderID="
        for i, oid in enumerate(ids, 1):
            try:
                out = orders_dir / f"order_{oid}.html"
                if out.exists():
                    print(f"[gc-pw] Skipping existing order details for {oid} → {out}")
                    continue
                page.goto(base + oid, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle")
                od_html = page.content()
                out.write_text(od_html, encoding="utf-8")
                print(f"[gc-pw] Saved order details {i}/{len(ids)} → {out}")
            except Exception as e:
                print(f"[gc-pw] Error fetching order {oid}: {e}")
        browser.close()
        today = dt.date.today()
        parsed_orders: List[dict] = []
        for oid in ids:
            f = (orders_dir / f"order_{oid}.html")
            try:
                html = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                html = ""
        gift_total, items = parse_order_details_html(html)
        if PARSER_DEBUG:
            _dump_parse_debug(orders_dir / f"order_{oid}.parse_debug.txt", html, gift_total, items)
        # Try to enrich with AmazonOrders API: real per-item prices and dates
        order_date_gc = _parse_order_date_from_html(html)
        order_date_from_api = None
        ship_date_from_api = None
        try:
            o = orders_api.get_order(oid)
            order_date_from_api = getattr(o, "order_date", None) or None
            for s in getattr(o, "shipments", []) or []:
                sd = getattr(s, "ship_date", None)
                if sd and (ship_date_from_api is None or sd < ship_date_from_api):
                    ship_date_from_api = sd
            api_items: List[Tuple[str, Decimal]] = []
            for s in getattr(o, "shipments", []) or []:
                for it in getattr(s, "items", []) or []:
                    title = getattr(it, "title", "") or "Item"
                    price = getattr(it, "price", None)
                    if price is None:
                        price = getattr(it, "item_price", 0)
                    try:
                        dprice = Decimal(str(price or 0))
                    except Exception:
                        dprice = Decimal("0")
                    if dprice != 0:
                        api_items.append((title, dprice))
            if api_items:
                items = api_items
        except Exception as e:
            if PARSER_DEBUG:
                print(f"[gc] API item/date fetch failed for {oid}: {e}")
            # Preserve original extracted prices in item dicts for memoing
            if gift_total > 0:
                scaled_items = _renormalize_items_to_total(items, gift_total)
            else:
                scaled_items = items
            ship_items = []
            for idx, (t, p_scaled) in enumerate(scaled_items):
                try:
                    orig = items[idx][1]
                except Exception:
                    orig = p_scaled
                ship_items.append({
                    "title": t,
                    "qty": 1,
                    "unit_price": str(p_scaled),
                    "orig_unit_price": str(orig),
                })
            # Try to parse order date from HTML; fallback to today
            od = _parse_order_date_from_html(html) or today
            shipments = [{
                "ship_date": od,
                "items": ship_items
            }]
            parsed_orders.append({
                "order_id": oid,
                "order_date": od,
                "shipments": shipments,
                "payment_method": "Amazon gift card balance",
            })
        print(f"[source] GC activity (Playwright) → parsed {len(parsed_orders)} orders with itemized splits (where possible)")
        return parsed_orders


def gc_collect_order_ids(session: AmazonSession, lookback_days: int, dump_dir: Path) -> List[str]:
    """Visit known GC balance/activity pages and extract Order IDs from the HTML.
    Saves the raw HTML in dump_dir for inspection. Returns unique order IDs (strings)."""
    dump_dir.mkdir(parents=True, exist_ok=True)
    found: List[str] = []
    today = dt.date.today()
    start_date = today - dt.timedelta(days=lookback_days)

    for idx, url in enumerate(GC_ACTIVITY_URLS, start=1):
        try:
            resp = session.get(url)
            html = _resp_text(resp)
            out = dump_dir / f"gc_activity_{idx}.html"
            out.write_text(html, encoding="utf-8")
            print(f"[gc] Saved GC activity page {idx} → {out}")
            ids = set(ORDER_ID_RE.findall(html))
            if ids:
                print(f"[gc] Found {len(ids)} order IDs on GC page {idx}.")
                found.extend(sorted(ids))
        except Exception as e:
            print(f"[gc] Error fetching {url}: {e}")

    # de-duplicate while preserving order
    seen = set()
    unique_ids: List[str] = []
    for oid in found:
        if oid not in seen:
            seen.add(oid)
            unique_ids.append(oid)
    return unique_ids


def gc_fetch_order_details(session: AmazonSession, order_ids: List[str], dump_dir: Path) -> None:
    """Fetch each order details page and save HTML for inspection."""
    dump_dir.mkdir(parents=True, exist_ok=True)
    base = "https://www.amazon.com/gp/your-account/order-details"
    for i, oid in enumerate(order_ids, start=1):
        try:
            out = dump_dir / f"order_{oid}.html"
            if out.exists():
                print(f"[gc] Skipping existing order details for {oid} → {out}")
                continue
            url = f"{base}?orderID={oid}"
            resp = session.get(url)
            html = _resp_text(resp)
            out.write_text(html, encoding="utf-8")
            print(f"[gc] Saved order details {i}/{len(order_ids)} → {out}")
        except Exception as e:
            print(f"[gc] Error fetching order {oid}: {e}")


def load_orders_from_gc_activity(lookback_days: int,
                                history_pages: Optional[int] = None,
                                history_page_size: Optional[int] = None,
                                skip_import_ids: Optional[set[str]] = None) -> List[dict]:
    """Primary GC Activity loader.

    - Logs in via AmazonSession and visits GC pages to collect order IDs.
    - Optionally augments the set of IDs with explicit order history pages for older orders.
    - For each order, prefers structured per‑item prices and dates from amazonorders API; otherwise
      falls back to HTML parsing and resilient heuristics.

    Returns a normalized list of orders ready for YNAB posting (with shipments/items)."""
    def _clean(v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip().strip('"').strip("'")

    session = AmazonSession(
        _clean(os.getenv("AMAZON_USERNAME")),
        _clean(os.getenv("AMAZON_PASSWORD")),
        otp_secret_key=_clean(os.getenv("AMAZON_OTP_SECRET_KEY")),
        debug=bool(os.getenv("AMZN_DEBUG")),
    )
    session.login()

    dump_base = Path(os.getenv("AMAZON_DUMP_DIR") or ".amazon_debug").expanduser()
    gc_dir = dump_base / "gc"
    orders_dir = dump_base / "orders"
    orders_api = AmazonOrders(session)

    ids = gc_collect_order_ids(session, lookback_days, gc_dir)
    # Optionally augment with order history pages (page 2/3, etc.)
    start_date = dt.date.today() - dt.timedelta(days=lookback_days)
    extra_ids = collect_order_ids_from_history_pages(session, start_date, dump_dir=dump_base / "history",
                                                    pages=history_pages, page_size=history_page_size)
    if extra_ids:
        # merge unique
        seen = set(ids)
        for oid in extra_ids:
            if oid not in seen:
                ids.append(oid)
                seen.add(oid)
    # Filter by existing import_ids unless updating
    upd = os.getenv("YNAB_UPDATE_EXISTING", "").lower() in ("1","true","yes")
    tag = (os.getenv("YNAB_IMPORT_ID_TAG") or "").strip().strip('"').strip("'")
    if skip_import_ids and not upd:
        filtered: List[str] = []
        for oid in ids:
            imp = f"YNAMAZON:{oid}" + (f":{tag}" if tag else "")
            if imp in skip_import_ids:
                print(f"[gc] Skipping already-posted import_id for {oid} ({imp})")
                continue
            filtered.append(oid)
        ids = filtered

    if not ids:
        print("[gc] No order IDs found on GC pages.")
        return []

    # Save details HTML for inspection
    gc_fetch_order_details(session, ids, orders_dir)

    # Parse each saved order details HTML into items + gift card total
    today = dt.date.today()
    parsed_orders: List[dict] = []
    for oid in ids:
        f = (orders_dir / f"order_{oid}.html")
        try:
            html = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            html = ""
        gift_total, items = parse_order_details_html(html)
        # Derive dates from HTML and optionally from API
        order_date_gc = _parse_order_date_from_html(html)
        order_date_from_api = None
        ship_date_from_api = None
        # Prefer structured per-item prices from the official parser, if available
        try:
            o = orders_api.get_order(oid)
            order_date_from_api = getattr(o, "order_date", None) or None
            api_items: List[Tuple[str, Decimal]] = []
            for s in getattr(o, "shipments", []) or []:
                sd = getattr(s, "ship_date", None)
                if sd and (ship_date_from_api is None or sd < ship_date_from_api):
                    ship_date_from_api = sd
                for it in getattr(s, "items", []) or []:
                    title = getattr(it, "title", "") or "Item"
                    price = getattr(it, "price", None)
                    if price is None:
                        price = getattr(it, "item_price", 0)
                    try:
                        dprice = Decimal(str(price or 0))
                    except Exception:
                        dprice = Decimal("0")
                    if dprice != 0:
                        api_items.append((title, dprice))
            if api_items:
                items = api_items
        except Exception as e:
            if PARSER_DEBUG:
                print(f"[gc] API item fetch failed for {oid}: {e}")
        if PARSER_DEBUG:
            _dump_parse_debug(orders_dir / f"order_{oid}.parse_debug.txt", html, gift_total, items)
        # Normalize items so they sum to the gift card total (if present),
        # but also preserve the originally extracted item price for memos.
        if gift_total > 0:
            scaled_items = _renormalize_items_to_total(items, gift_total)
        else:
            scaled_items = items

        ship_items = []
        for idx, (t, p_scaled) in enumerate(scaled_items):
            try:
                orig = items[idx][1]
            except Exception:
                orig = p_scaled
            ship_items.append({
                "title": t,
                "qty": 1,
                "unit_price": str(p_scaled),
                "orig_unit_price": str(orig),
            })

        # Choose dates: prefer earliest ship date from API, else order date (API/HTML), else today
        _od = (order_date_from_api or order_date_gc or today)
        _sd = (ship_date_from_api or _od)
        shipments = [{
            "ship_date": _sd,
            "items": ship_items
        }]

        parsed_orders.append({
            "order_id": oid,
            "order_date": _od,
            "shipments": shipments if shipments[0]["items"] else [{"ship_date": _od, "items": []}],
            "payment_method": "Amazon gift card balance",
        })
    print(f"[source] GC activity → parsed {len(parsed_orders)} orders with itemized splits (where possible)")
    return parsed_orders

def load_orders_from_csv(csv_path: str, lookback_days: int) -> List[dict]:
    p = Path(csv_path)
    if not p.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    today = dt.date.today()
    start_date = today - dt.timedelta(days=lookback_days)

    normalized: List[dict] = []
    with p.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Try to be resilient to column name variants across Amazon regions
            order_id = (row.get("order-id") or row.get("Order ID") or row.get("Order ID ") or row.get("Order Number") or "").strip()
            order_date_str = (row.get("order-date") or row.get("Order Date") or row.get("Purchase Date") or "").strip()
            title = (row.get("title") or row.get("Title") or row.get("Item Name") or "Item").strip()
            qty_str = (row.get("quantity") or row.get("Quantity") or "1").strip()
            unit_price_str = (row.get("item-price") or row.get("Item Subtotal") or row.get("Item Total") or row.get("Item Total (USD)") or row.get("Item Price") or "0").strip()
            pm = (row.get("payment-instrument-type") or row.get("Payment Instrument Type") or row.get("Payment Method") or "").strip()

            if not order_id or not order_date_str:
                continue

            # Parse date (Amazon CSV often ISO or locale). Try a few formats.
            d = None
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y"):
                try:
                    d = dt.datetime.strptime(order_date_str.split(" ")[0], fmt).date()
                    break
                except ValueError:
                    pass
            if d is None or d < start_date:
                continue

            try:
                qty = Decimal(qty_str)
            except Exception:
                qty = Decimal("1")
            try:
                unit = Decimal(str(unit_price_str).replace("$", "").replace(",", ""))
            except Exception:
                unit = Decimal("0")

            shipments = [{
                "ship_date": d,
                "items": [{
                    "title": title,
                    "qty": int(qty),
                    "unit_price": str(unit),
                }],
            }]

            normalized.append({
                "order_id": order_id,
                "order_date": d,
                "shipments": shipments,
                "payment_method": pm,
            })
    return normalized


# Helper: Load only the Amazon Transactions API (no order scraper, no CSV)
def load_amazon_transactions_only(lookback_days: int) -> List[dict]:
    def _clean(v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip().strip('"').strip("'")
    session = AmazonSession(
        _clean(os.getenv("AMAZON_USERNAME")),
        _clean(os.getenv("AMAZON_PASSWORD")),
        otp_secret_key=_clean(os.getenv("AMAZON_OTP_SECRET_KEY")),
        debug=bool(os.getenv("AMZN_DEBUG"))
    )
    session.login()
    today = dt.date.today()
    start_date = today - dt.timedelta(days=lookback_days)
    tx_api = AmazonTransactions(session)
    tx_list = tx_api.get_transactions(days=lookback_days)
    normalized_tx: List[dict] = []
    for tx in tx_list:
        tx_date = getattr(tx, "date", None)
        if not tx_date or tx_date < start_date:
            continue
        amt = getattr(tx, "amount", 0) or 0
        order_id = getattr(tx, "order_number", None) or getattr(tx, "order_id", None)
        desc = getattr(tx, "description", "Amazon Transaction") or "Amazon Transaction"
        shipments = [{
            "ship_date": tx_date,
            "items": [{
                "title": desc,
                "qty": 1,
                "unit_price": str(amt),
            }],
        }]
        normalized_tx.append({
            "order_id": order_id or f"TX-{tx_date.isoformat()}-{abs(int(amt*100))}",
            "order_date": tx_date,
            "shipments": shipments,
            "payment_method": getattr(tx, "payment_method", None) or "Amazon Transaction",
        })
    return normalized_tx


def to_milliunits(amount_decimal: Decimal) -> int:
    # YNAB uses milliunits (e.g., $1.23 -> 1230)
    return int((amount_decimal * Decimal(1000)).to_integral_value(rounding=ROUND_HALF_UP))



def best_txn_date(order: dict) -> dt.date:
    ship_dates = []
    for s in order.get("shipments", []):
        if s.get("ship_date"):
            ship_dates.append(s["ship_date"])
    if ship_dates:
        return min(ship_dates)
    return order.get("order_date") or dt.date.today()


# Helper: Compute the total of an order as Decimal (rounded to 2 dp)
def order_total_decimal(order: dict) -> Decimal:
    total = Decimal("0.00")
    for shipment in order.get("shipments", []):
        for item in shipment.get("items", []):
            qty = Decimal(str(item.get("qty", 1)))
            unit = Decimal(str(item.get("unit_price", "0")))
            total += (qty * unit)
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def build_split_for_order(order: dict,
                          category_rules: Dict[str, str],
                          default_category_id: Optional[str] = None
                          ) -> Tuple[int, List[dict], str]:
    """
    Returns (total_milliunits, subtransactions, memo)
    """
    subs: List[dict] = []
    grand_total = Decimal("0.00")
    line_memos = []

    order_id_for_memo = order.get("order_id", "")
    for shipment in order.get("shipments", []):
        for item in shipment.get("items", []):
            title = str(item.get("title", "")).strip() or "Item"
            qty = Decimal(str(item.get("qty", 1)))
            unit_price = Decimal(str(item.get("unit_price", "0")))
            line_total = (qty * unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            grand_total += line_total

            # Leave all subtransactions uncategorized so user can categorize them
            cat_id = None

            # Include original extracted unit price (if present) to help manual adjustments
            orig_unit_price = None
            try:
                if "orig_unit_price" in item:
                    orig_unit_price = Decimal(str(item.get("orig_unit_price")))
            except Exception:
                orig_unit_price = None
            # Memo: item name first, then pricing, then order id at the end
            memo_parts = [f"{title} x{int(qty)} @ {unit_price}"]
            # Always include the originally extracted per-item price if we have it,
            # so you can see the receipt price even when normalization re-scales.
            if orig_unit_price is not None:
                try:
                    memo_parts.append(f"(orig {orig_unit_price.quantize(Decimal('0.01'))})")
                except Exception:
                    memo_parts.append(f"(orig {item.get('orig_unit_price')})")
            if order_id_for_memo:
                memo_parts.append(f"| Amazon Order {order_id_for_memo}")
            subs.append({
                "amount": to_milliunits(line_total) * -1,  # outflow is negative in YNAB
                "category_id": cat_id,
                "memo": _truncate_memo(" ".join(memo_parts), 500),
            })
            line_memos.append(f"{title} (${line_total})")

    total_milli = to_milliunits(grand_total) * -1
    # Parent memo: item names first, order id at the end
    parent_memo = "; ".join(line_memos[:8])
    if len(line_memos) > 8:
        parent_memo += f"; +{len(line_memos)-8} more..."
    if order_id_for_memo:
        parent_memo = (parent_memo + f" | Amazon Order {order_id_for_memo}").strip()
    parent_memo = _truncate_memo(parent_memo, 500)
    return total_milli, subs, parent_memo


def main():
    load_dotenv()
    # CLI args: allow selecting history pages for older orders
    try:
        import argparse
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("-p", "--history-pages", type=int, dest="history_pages")
        parser.add_argument("--history-page-size", type=int, dest="history_page_size")
        # Parse known args and leave the rest (so we don't break anything)
        args, _ = parser.parse_known_args()
        _history_pages = args.history_pages
        _history_page_size = args.history_page_size
    except Exception:
        _history_pages = None
        _history_page_size = None

    # Sanitize env values (strip quotes/whitespace)
    def _clean(v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip().strip('"').strip("'")

    ynab_api_key = _clean(os.environ.get("YNAB_API_KEY"))
    budget_id = _clean(os.environ.get("YNAB_BUDGET_ID"))
    account_id = _clean(os.environ.get("YNAB_TARGET_ACCOUNT_ID"))

    if not ynab_api_key or not budget_id or not account_id:
        missing = [k for k, v in {
            "YNAB_API_KEY": ynab_api_key,
            "YNAB_BUDGET_ID": budget_id,
            "YNAB_TARGET_ACCOUNT_ID": account_id,
        }.items() if not v]
        print(f"Missing required env var(s): {', '.join(missing)}. Check your .env file and try again.")
        return

    payee_id = _clean(os.environ.get("YNAB_PAYEE_ID_AMAZON"))
    payee_name = _clean(os.environ.get("YNAB_PAYEE_NAME_AMAZON")) or "Amazon"

    # Debug mode: creates transactions with blue flag, unique import_id, forces 10-day lookback
    debug_mode = os.getenv("YNAMAZON_DEBUG", "").lower() in ("1", "true", "yes")
    if debug_mode:
        import time as _time
        debug_tag = f"d{int(_time.time()) % 1000000}"  # Unique tag based on timestamp
        lookback_days = 10
        print(f"[DEBUG MODE] ON - transactions will have BLUE flag, import_id tag: {debug_tag}, lookback: {lookback_days} days")
    else:
        debug_tag = None
        lookback_days = int(_clean(os.environ.get("YNAB_AMAZON_LOOKBACK_DAYS") or "45"))
    
    category_rules_json = _clean(os.environ.get("YNAB_CATEGORY_RULES_JSON") or "{}")
    category_rules: Dict[str, str] = json.loads(category_rules_json)

    default_category_id = _clean(os.environ.get("YNAB_DEFAULT_CATEGORY_ID"))  # optional

    gift_card_account_id = _clean(os.environ.get("YNAB_GIFT_CARD_ACCOUNT_ID"))  # optional separate account for gift card spend
    default_cc_account_id = _clean(os.environ.get("YNAB_DEFAULT_CREDIT_CARD_ACCOUNT_ID"))  # optional default credit card account
    # Optional JSON mapping of card last-4-digits to YNAB account IDs
    cc_accounts_json = _clean(os.environ.get("YNAB_CREDIT_CARD_ACCOUNTS") or "{}")
    try:
        cc_accounts_map: Dict[str, str] = json.loads(cc_accounts_json)
    except json.JSONDecodeError:
        print(f"Warning: YNAB_CREDIT_CARD_ACCOUNTS is not valid JSON, ignoring. Value: {cc_accounts_json}")
        cc_accounts_map = {}
    csv_path = _clean(os.environ.get("AMAZON_ORDER_REPORT_CSV"))  # optional CSV fallback

    if os.getenv("AMZN_DEBUG"):
        print("[amazon-orders] HTML debug is ON (library will save request/response HTML snapshots).")
    if os.getenv("AMAZON_INSPECT"):
        print("[inspect] Amazon order inspect mode is ON. Raw + normalized details will print to terminal.")
    if os.getenv("AMAZON_DUMP_HISTORY_HTML"):
        print("[dump] History HTML dump is ON; pages will be written to ./.amazon_debug/")
    
    # New payment transactions mode - recommended for tracking both GC and CC purchases
    use_payment_transactions = os.getenv("AMAZON_USE_PAYMENT_TRANSACTIONS", "").lower() in ("1", "true", "yes")
    if use_payment_transactions:
        print("[payments] Payment transactions mode is ON (using yourpayments/transactions page).")
    
    use_gc_activity = os.getenv("AMAZON_GC_ACTIVITY", "").lower() in ("1", "true", "yes")
    if use_gc_activity:
        print("[gc] Gift-Card Activity mode is ON (collect order IDs from GC pages and fetch details HTML).")
    use_gc_activity_pw = os.getenv("AMAZON_GC_PLAYWRIGHT", "").lower() in ("1","true","yes")
    if use_gc_activity_pw:
        print("[gc-pw] Playwright GC mode is ON (interactive scrape of GC + order details).")
    
    # Print account configuration
    if gift_card_account_id:
        print(f"[accounts] Gift card account configured: {gift_card_account_id[:8]}...")
    if default_cc_account_id:
        print(f"[accounts] Default credit card account configured: {default_cc_account_id[:8]}...")
    if cc_accounts_map:
        print(f"[accounts] Credit card mapping configured for cards ending in: {', '.join(cc_accounts_map.keys())}")

    force_tx_only = os.getenv("AMAZON_SCRAPE_DISABLE", "").lower() in ("1", "true", "yes")

    # Connect YNAB
    cfg = ynab.Configuration()
    # Set BOTH styles to be robust across ynab SDK versions
    cfg.access_token = ynab_api_key
    cfg.api_key["Authorization"] = ynab_api_key
    cfg.api_key_prefix["Authorization"] = "Bearer"
    cfg.host = "https://api.youneedabudget.com/v1"

    # Removed SDK client setup:
    # api_client = ynab.ApiClient(cfg)
    # tx_api = ynab.TransactionsApi(api_client)

    # Safe debug to confirm what's being used (no secrets printed)
    print(f"YNAB token length: {len(ynab_api_key)} | budget: {budget_id} | account: {account_id}")
    import hashlib as _hashlib
    print("YNAB token sha256:", _hashlib.sha256(ynab_api_key.encode()).hexdigest())

    ynab_session = requests.Session()
    ynab_session.trust_env = False

    # Preflight 1: Raw HTTP (bypasses SDK). If this passes, token is good.
    try:
        r = ynab_session.get(
            "https://api.youneedabudget.com/v1/user",
            headers={
                "Authorization": f"Bearer {ynab_api_key}",
                "Accept": "*/*",
                "User-Agent": "curl/8.6.0",
            },
            timeout=10,
        )
        if r.status_code == 401:
            print(f"YNAB raw preflight 401. Body: {r.text}")
            return
        elif r.status_code >= 400:
            print(f"YNAB raw preflight error {r.status_code}. Body: {r.text}")
            return
    except Exception as e:
        print(f"YNAB preflight HTTP exception: {e}")
        return

    BASE = "https://api.youneedabudget.com/v1"
    AUTH_HEADER = {"Authorization": f"Bearer {ynab_api_key}", "Accept": "application/json"}

    def ynab_get_existing_import_ids(budget_id: str, account_id: str, since_date: str) -> set[str]:
        url = f"{BASE}/budgets/{budget_id}/accounts/{account_id}/transactions"
        params = {"since_date": since_date}
        out: set[str] = set()
        r = ynab_session.get(url, headers=AUTH_HEADER, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        for t in data.get("data", {}).get("transactions", []):
            imp = t.get("import_id")
            if imp:
                out.add(imp)
        return out

    def ynab_get_existing_by_import_id(budget_id: str, account_id: str, since_date: str) -> dict[str, dict]:
        """Return a map of import_id -> full transaction (includes 'id').

        This uses the budget‑wide transactions endpoint so we can find placeholders in
        any account (not just the target account). Useful when you’ve manually moved
        or created staging placeholders in a different account.
        """
        # Use the all-transactions endpoint so we can find placeholders in other accounts.
        url = f"{BASE}/budgets/{budget_id}/transactions"
        params = {"since_date": since_date}
        out: dict[str, dict] = {}
        r = ynab_session.get(url, headers=AUTH_HEADER, params=params, timeout=30)
        try:
            r.raise_for_status()
        except Exception:
            print(f"YNAB existing lookup error: {r.status_code} {r.text}")
            return out
        data = r.json()
        for t in data.get("data", {}).get("transactions", []):
            imp = t.get("import_id")
            if imp:
                out[imp] = t
        return out

    def ynab_get_account_balance(budget_id: str, account_id: str) -> Optional[Decimal]:
        """Get the cleared balance for a YNAB account in dollars."""
        url = f"{BASE}/budgets/{budget_id}/accounts/{account_id}"
        r = ynab_session.get(url, headers=AUTH_HEADER, timeout=20)
        try:
            r.raise_for_status()
        except Exception:
            print(f"YNAB account lookup error: {r.status_code} {r.text}")
            return None
        data = r.json()
        account = data.get("data", {}).get("account", {})
        # YNAB returns balance in milliunits (1000 = $1.00)
        cleared_balance_milli = account.get("cleared_balance", 0)
        return Decimal(cleared_balance_milli) / 1000
    
    def ynab_get_uncleared_transactions(budget_id: str, account_id: str) -> list[dict]:
        """Get all uncleared transactions for an account."""
        url = f"{BASE}/budgets/{budget_id}/accounts/{account_id}/transactions"
        r = ynab_session.get(url, headers=AUTH_HEADER, timeout=30)
        try:
            r.raise_for_status()
        except Exception:
            print(f"YNAB transactions lookup error: {r.status_code} {r.text}")
            return []
        data = r.json()
        txns = data.get("data", {}).get("transactions", [])
        return [t for t in txns if t.get("cleared") != "reconciled"]
    
    def ynab_reconcile_transactions(budget_id: str, txn_ids: list[str]) -> int:
        """Mark transactions as reconciled. Returns count of successfully reconciled."""
        reconciled = 0
        for txn_id in txn_ids:
            url = f"{BASE}/budgets/{budget_id}/transactions/{txn_id}"
            payload = {"transaction": {"cleared": "reconciled"}}
            r = ynab_session.put(url, headers=AUTH_HEADER, json=payload, timeout=20)
            if r.status_code == 200:
                reconciled += 1
            else:
                print(f"  Failed to reconcile {txn_id}: {r.status_code}")
        return reconciled

    def ynab_create_or_update_transactions(budget_id: str, account_id: str, txns: list[dict], since_date: Optional[str] = None) -> list[dict]:
        """Create or update transactions.

        Behavior:
          - Always truncates parent/sub memos to 500 characters.
          - If YNAB_UPDATE_EXISTING is not set, performs a simple bulk create.
          - If YNAB_UPDATE_EXISTING is enabled, looks up existing transactions by import_id since
            `since_date` and splits the batch into updates and creates.
          - Update guardrails (env) protect approved/categorized/with‑splits transactions so your
            finalized work is never overwritten unless you explicitly opt out.

        Returns a flattened list of transactions resulting from PATCH + POST.
        """
        # Defensive pass: ensure memo fields respect YNAB's max length (observed 500 chars).
        safe_txns: list[dict] = []
        for t in txns:
            t = dict(t)  # shallow copy
            if "memo" in t and isinstance(t["memo"], str):
                t["memo"] = _truncate_memo(t["memo"], 500)
            if "subtransactions" in t and isinstance(t["subtransactions"], list):
                safe_subs = []
                for st in t["subtransactions"]:
                    st = dict(st)
                    if "memo" in st and isinstance(st["memo"], str):
                        st["memo"] = _truncate_memo(st["memo"], 500)
                    safe_subs.append(st)
                t["subtransactions"] = safe_subs
            safe_txns.append(t)

        update_enabled = (os.getenv("YNAB_UPDATE_EXISTING", "").lower() in ("1", "true", "yes"))

        # If not updating, just bulk create as before.
        if not update_enabled:
            url = f"{BASE}/budgets/{budget_id}/transactions"
            body = {"transactions": safe_txns}
            r = ynab_session.post(
                url,
                headers={**AUTH_HEADER, "Content-Type": "application/json"},
                json=body,
                timeout=30,
            )
            try:
                r.raise_for_status()
            except Exception:
                print(f"YNAB create_transaction error: {r.status_code} {r.text}")
                return []
            data = r.json()
            return data.get("data", {}).get("transactions", [])

        # Determine since_date for lookup if not provided: use earliest date or env lookback window
        if not since_date:
            try:
                earliest = min(dt.date.fromisoformat(t.get("date")) for t in safe_txns if t.get("date"))
                since_date = earliest.isoformat()
            except Exception:
                since_date = (dt.date.today() - dt.timedelta(days=120)).isoformat()

        existing_map = ynab_get_existing_by_import_id(budget_id, account_id, since_date)

        # Update guards to avoid overwriting your finalized work
        only_unapproved = os.getenv("YNAB_UPDATE_ONLY_UNAPPROVED", "true").lower() in ("1","true","yes")
        only_uncategorized = os.getenv("YNAB_UPDATE_ONLY_UNCATEGORIZED", "true").lower() in ("1","true","yes")
        only_empty_splits = os.getenv("YNAB_UPDATE_ONLY_EMPTY_SPLITS", "true").lower() in ("1","true","yes")
        only_payee_name = os.getenv("YNAB_UPDATE_ONLY_PAYEE_NAME") or os.getenv("YNAB_PAYEE_NAME_TO_BE_PROCESSED")

        def _eligible(existing_tx: dict) -> bool:
            try:
                if only_unapproved and existing_tx.get("approved"):
                    return False
                if only_uncategorized and existing_tx.get("category_id"):
                    return False
                subs = existing_tx.get("subtransactions") or []
                if only_empty_splits and len(subs) > 0:
                    return False
                if only_payee_name:
                    if (existing_tx.get("payee_name") or "") != only_payee_name:
                        return False
            except Exception:
                pass
            return True

        to_update: list[tuple[str, dict]] = []  # (transaction_id, txn)
        to_create: list[dict] = []
        skipped_updates = 0
        for t in safe_txns:
            imp = t.get("import_id")
            if imp and imp in existing_map:
                existing_tx = existing_map[imp]
                tx_id = existing_tx.get("id")
                if tx_id and _eligible(existing_tx):
                    to_update.append((tx_id, t))
                else:
                    skipped_updates += 1
                    to_create.append(t)  # creating a separate entry keeps your original intact
            else:
                to_create.append(t)

        print(f"YNAB upsert: to_update={len(to_update)} to_create={len(to_create)} (since_date={since_date})")
        if skipped_updates:
            print(f"YNAB upsert guard: skipped {skipped_updates} existing txns (approved/categorized/split/payee mismatch)")

        results: list[dict] = []
        # PATCH updates one-by-one to ensure proper YNAB handling
        for tx_id, t in to_update:
            url = f"{BASE}/budgets/{budget_id}/transactions/{tx_id}"
            body = {"transaction": t}
            r = ynab_session.patch(
                url,
                headers={**AUTH_HEADER, "Content-Type": "application/json"},
                json=body,
                timeout=30,
            )
            try:
                r.raise_for_status()
            except Exception:
                print(f"YNAB update_transaction error: {r.status_code} {r.text}")
                continue
            data = r.json()
            tx = data.get("data", {}).get("transaction")
            if tx:
                results.append(tx)
        if to_update:
            print(f"YNAB update: updated {len(results)} transactions")

        # POST any remaining creates in bulk
        if to_create:
            url = f"{BASE}/budgets/{budget_id}/transactions"
            body = {"transactions": to_create}
            r = ynab_session.post(
                url,
                headers={**AUTH_HEADER, "Content-Type": "application/json"},
                json=body,
                timeout=30,
            )
            try:
                r.raise_for_status()
            except Exception:
                print(f"YNAB create_transaction error: {r.status_code} {r.text}")
                return results
            data = r.json()
            created = data.get("data", {}).get("transactions", []) or []
            results.extend(created)
            duplicates = (data.get("data", {}) or {}).get("duplicate_import_ids")
            if duplicates:
                preview = ", ".join(list(duplicates)[:5])
                print(f"YNAB create: duplicate_import_ids={len(duplicates)} (showing up to 5): {preview}")
            print(f"YNAB create: requested {len(to_create)}, created {len(created)} (status={r.status_code})")

        return results

    def ynab_create_dummy_transaction(budget_id: str, account_id: str) -> None:
        """Create a $1.00 outflow test transaction you can delete later."""
        today = dt.date.today().isoformat()
        import time as _t
        imp = f"YNAMAZON:DUMMY:{int(_t.time())}"
        txn = {
            "account_id": account_id,
            "date": today,
            "amount": -1000,  # -$1.00 in milliunits
            "memo": _truncate_memo("Connectivity test (safe to delete)", 500),
            "cleared": "cleared",
            "approved": False,
            "import_id": imp,
        }
        url = f"{BASE}/budgets/{budget_id}/transactions"
        r = ynab_session.post(
            url,
            headers={**AUTH_HEADER, "Content-Type": "application/json"},
            json={"transactions": [txn]},
            timeout=30,
        )
        if r.status_code >= 400:
            print(f"YNAB dummy create error: {r.status_code} {r.text}")
            return
        d = r.json().get("data", {})
        created = d.get("transactions") or []
        dups = d.get("duplicate_import_ids") or []
        if created:
            t = created[0]
            print(f"YNAB dummy created: id={t.get('id')} date={t.get('date')} amount={t.get('amount')} memo={t.get('memo')}")
        elif dups:
            print(f"YNAB dummy duplicate import_id (not created): {imp}")
        else:
            print(f"YNAB dummy: no transaction returned (status={r.status_code}) body={r.text[:240]}")

    # Removed Preflight 2 block that calls ynab.UserApi(api_client).get_user()

    # Optional: connectivity test (creates a $1 dummy transaction and exits)
    if os.getenv("YNAB_CREATE_DUMMY_TEST", "").lower() in ("1", "true", "yes"):
        print("Creating YNAB dummy transaction for connectivity test...")
        ynab_create_dummy_transaction(budget_id, account_id)
        return

    # Build a budget-wide skip set of existing import_ids for loader filtering
    try:
        _since = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
        _existing_map_for_skip = ynab_get_existing_by_import_id(budget_id, account_id, _since)
        _skip_import_ids: set[str] = set(_existing_map_for_skip.keys())
    except Exception:
        _skip_import_ids = set()

    # Load recent Amazon orders
    orders: List[dict] = []
    
    # Combine multiple data sources and deduplicate by order_id
    def merge_orders(existing: List[dict], new_orders: List[dict], source_name: str) -> List[dict]:
        """Merge new orders into existing list, avoiding duplicates by order_id."""
        existing_ids = {o["order_id"] for o in existing}
        added = 0
        for order in new_orders:
            if order["order_id"] not in existing_ids:
                existing.append(order)
                existing_ids.add(order["order_id"])
                added += 1
        if added > 0:
            print(f"[merge] Added {added} new orders from {source_name} (skipped {len(new_orders) - added} duplicates)")
        return existing
    
    # Source 1: Payment transactions mode (recommended for GC + CC tracking)
    if use_payment_transactions:
        payment_orders = load_orders_from_payment_transactions(lookback_days, skip_import_ids=_skip_import_ids)
        orders = merge_orders(orders, payment_orders, "payment transactions")
    
    # Source 2: Gift card activity (can catch orders not yet on payment page)
    if use_gc_activity_pw:
        gc_pw_orders = load_orders_from_gc_playwright(lookback_days, skip_import_ids=_skip_import_ids)
        orders = merge_orders(orders, gc_pw_orders, "gift card playwright")
    
    if use_gc_activity:
        gc_orders = load_orders_from_gc_activity(lookback_days, history_pages=_history_pages, history_page_size=_history_page_size, skip_import_ids=_skip_import_ids)
        orders = merge_orders(orders, gc_orders, "gift card activity")
    
    # Source 3: CSV fallback
    if not orders and csv_path:
        try:
            csv_orders = load_orders_from_csv(csv_path, lookback_days)
            orders = merge_orders(orders, csv_orders, "CSV report")
        except Exception as e:
            print(f"CSV load failed ({e}); falling back to other sources.")
    
    # Source 4: Other fallbacks if still no orders
    if not orders and force_tx_only:
        orders = load_amazon_transactions_only(lookback_days)
        print(f"[source] Amazon Transactions API only → {len(orders)} entries")
    if not orders and not force_tx_only:
        try:
            orders = load_amazon_orders(lookback_days)
        except NotImplementedError as e:
            print(e)
            return
        except Exception as e:
            print(f"Orders scraper failed ({e}); trying Transactions API.")
            orders = load_amazon_transactions_only(lookback_days)
            print(f"[source] Amazon Transactions API fallback → {len(orders)} entries")

    # Optional: list the Amazon orders we found (one line each with total)
    if (os.getenv("YNAB_LIST_AMAZON_ORDERS", "").lower() in ("1", "true", "yes")):
        print(f"Found {len(orders)} Amazon orders in lookback:")
        for o in sorted(orders, key=lambda x: best_txn_date(x)):
            total = order_total_decimal(o)
            dt_str = best_txn_date(o).isoformat()
            pm = o.get("payment_method") or ""
            print(f"{dt_str}  {o['order_id']}  total=${total}  pay='{pm}'")

    # Build a set of import_ids already present (to be idempotent when not updating)
    # Use a longer lookback for existing check to catch duplicates even in debug mode
    ynab_existing_lookback = max(lookback_days, 90)  # At least 90 days to catch older transactions
    since_date = (dt.date.today() - dt.timedelta(days=ynab_existing_lookback)).isoformat()
    # IMPORTANT: Use budget-wide existing lookup, not account-scoped.
    # We may post transactions to a different account (e.g., YNAB_GIFT_CARD_ACCOUNT_ID),
    # and account-scoped lookup would miss those and re-create duplicates.
    # Always fetch fresh from budget-wide endpoint to catch all existing transactions
    existing_import_ids = set()
    try:
        fresh_map = ynab_get_existing_by_import_id(budget_id, account_id, since_date)
        existing_import_ids = set(fresh_map.keys())
        yna_count = len([k for k in existing_import_ids if k.startswith('YNA') or k.startswith('YNAMAZON')])
        print(f"[ynab] Found {len(existing_import_ids)} existing transactions ({yna_count} YNA/YNAMAZON)")
    except Exception as e:
        print(f"[ynab] Warning: Could not fetch existing import_ids: {e}")

    to_create = []

    update_existing = (os.getenv("YNAB_UPDATE_EXISTING", "").lower() in ("1", "true", "yes"))

    # Optional: allow changing import_id if recreating after deletions (YNAB de-dup persists for a while)
    import_id_tag = _clean(os.environ.get("YNAB_IMPORT_ID_TAG"))  # e.g., "v2" or date stamp
    # In debug mode, use the debug_tag to ensure unique import_ids
    if debug_mode and debug_tag:
        import_id_tag = debug_tag

    def _build_import_id(order_id: str) -> str:
        """Build import_id, respecting YNAB's 36 character limit."""
        # YNAB limit is 36 chars. Order IDs like "111-1234567-1234567:p0" can be 22 chars
        # Use shorter prefix and compact format
        prefix = "YNA"  # 3 chars
        # Remove dashes from order_id to save space: "111-1234567-1234567" -> "11112345671234567"
        # But keep :p0/:p1 suffix if present
        if ":p" in order_id:
            base_id, suffix = order_id.rsplit(":p", 1)
            compact_id = base_id.replace("-", "") + "p" + suffix
        else:
            compact_id = order_id.replace("-", "")
        
        if import_id_tag:
            result = f"{prefix}:{compact_id}:{import_id_tag}"
        else:
            result = f"{prefix}:{compact_id}"
        
        # Truncate if still too long
        if len(result) > 36:
            result = result[:36]
        return result

    # Regex to extract last 4 digits from credit card payment methods
    CARD_LAST4_RE = re.compile(r'\*{4}(\d{4})')
    
    def _get_account_for_payment(pm_text: str) -> Tuple[str, str]:
        """Returns (account_id, label) for a payment method string."""
        pm_lower = pm_text.lower()
        
        # Check for gift card payment
        if gift_card_account_id and ("gift card" in pm_lower or pm_lower == "amazon gift card balance"):
            return gift_card_account_id, "gift card"
        
        # Try to extract credit card last 4 digits
        card_match = CARD_LAST4_RE.search(pm_text)
        if card_match:
            last4 = card_match.group(1)
            if last4 in cc_accounts_map:
                return cc_accounts_map[last4], f"credit card (mapped:{last4})"
            elif default_cc_account_id:
                return default_cc_account_id, "credit card (default)"
        elif default_cc_account_id and any(kw in pm_lower for kw in ("visa", "mastercard", "amex", "discover", "credit", "debit")):
            return default_cc_account_id, "credit card (default)"
        
        # Fallback to the main target account
        return account_id, "default"

    def _check_order_exists(order_id: str) -> bool:
        """Check if order already exists using any known import_id format."""
        # Strip :p0/:p1 suffix to get base order ID
        base_order_id = order_id.split(":p")[0] if ":p" in order_id else order_id
        
        # Check for any existing import_id that contains this order ID (with or without dashes)
        # This catches: YNAMAZON:111-4689615-9165062:v14, YNA:11146896159165062:d322436, etc.
        for existing_id in existing_import_ids:
            # Check if base order ID (with dashes) is in the existing import_id
            if base_order_id in existing_id:
                return True
            # Check if compact order ID (without dashes) is in the existing import_id
            compact_id = base_order_id.replace("-", "")
            if compact_id in existing_id:
                return True
        
        return False

    skipped_count = 0
    for order in orders:
        order_id = order["order_id"]
        txn_date = best_txn_date(order)
        import_id = _build_import_id(order_id)
        
        # Check if this order already exists (using any import_id format)
        # In debug mode, always create fresh transactions (skip duplicate check)
        if not debug_mode and _check_order_exists(order_id) and not update_existing:
            skipped_count += 1
            continue  # already created and not updating

        total_milli, sublines, parent_memo = build_split_for_order(
            order, category_rules, default_category_id
        )

        # Determine target account based on payment method
        pm_text = order.get("payment_method") or ""
        post_account_id, acct_label = _get_account_for_payment(pm_text)
        
        # Credit card transactions should be uncleared (bank will import the actual charge)
        # Gift card transactions can be cleared since they're from our tracking
        is_gift_card = "gift card" in pm_text.lower()
        cleared_status = "cleared" if is_gift_card else "uncleared"
        
        # Use the base order ID for memo if available (for split payments)
        memo_order_id = order.get("order_id_base", order_id)
        
        print(f"[routing] Order {order_id}: payment='{pm_text}' → {acct_label} account ({cleared_status})")

        parent = {
            "account_id": post_account_id,
            "date": txn_date.isoformat(),
            "amount": total_milli,        # parent should equal sum(subs)
            "memo": parent_memo,
            "cleared": cleared_status,
            "approved": False,            # let you approve in YNAB
            "import_id": import_id,
        }
        
        # Only use split transactions if there's more than 1 item
        if len(sublines) > 1:
            parent["category_id"] = None  # required for split parents
            parent["subtransactions"] = sublines
        else:
            # Single item - use regular transaction (no split), leave uncategorized
            parent["category_id"] = None
        
        # Add flag color: blue for debug mode, green for normal mode
        if debug_mode:
            parent["flag_color"] = "blue"
        else:
            parent["flag_color"] = "green"
        if payee_id:
            parent["payee_id"] = payee_id
        else:
            parent["payee_name"] = payee_name

        to_create.append(parent)

    if skipped_count > 0:
        print(f"[skip] Skipped {skipped_count} orders that already exist in YNAB.")
    
    if not to_create:
        print("Hint: set YNAB_UPDATE_EXISTING=true to update any existing placeholder transactions by import_id.")
        print("No missing orders to create. (Nothing new or all already posted.)")
        if csv_path:
            print("Note: Gift-card-only orders often don’t appear in Amazon’s Transactions feed; CSV mode helps capture them.")
        else:
            print("Tip: Set AMAZON_ORDER_REPORT_CSV=/path/to/OrderHistory.csv or set AMAZON_SCRAPE_DISABLE=true to use the Transactions API directly.")
        # Don't return - still run reconciliation below

    # POST create (only if there are transactions to create)
    if to_create:
        created = ynab_create_or_update_transactions(budget_id, account_id, to_create, since_date)
        if created:
            print(f"Created {len(created)} YNAB transaction(s).")
            for t in created:
                print(f"- {t['date']} {t['memo']}  amount={t['amount']}  id={t['id']}")

    # Gift card balance reconciliation
    reconcile_gc = os.getenv("YNAB_RECONCILE_GC_BALANCE", "").lower() in ("1", "true", "yes")
    if reconcile_gc and gift_card_account_id:
        print("\n[reconcile] Checking gift card balance...")
        
        # Get Amazon's reported balance from saved HTML
        gc_html_path = Path(".amazon_debug/gc/gc_activity_1.html")
        amazon_balance = get_amazon_gc_balance_from_file(gc_html_path)
        
        if amazon_balance is None:
            print("[reconcile] Could not read Amazon gift card balance from HTML.")
        else:
            print(f"[reconcile] Amazon reports gift card balance: ${amazon_balance}")
            
            # Get YNAB account balance
            ynab_balance = ynab_get_account_balance(budget_id, gift_card_account_id)
            if ynab_balance is None:
                print("[reconcile] Could not get YNAB account balance.")
            else:
                print(f"[reconcile] YNAB cleared balance: ${ynab_balance}")
                
                # Compare balances
                if amazon_balance == ynab_balance:
                    print("[reconcile] ✓ Balances match! Reconciling cleared transactions...")
                    
                    # Get all non-reconciled transactions and mark them reconciled
                    unreconciled = ynab_get_uncleared_transactions(budget_id, gift_card_account_id)
                    cleared_txns = [t for t in unreconciled if t.get("cleared") == "cleared"]
                    
                    if cleared_txns:
                        txn_ids = [t["id"] for t in cleared_txns]
                        count = ynab_reconcile_transactions(budget_id, txn_ids)
                        print(f"[reconcile] Reconciled {count} transaction(s).")
                    else:
                        print("[reconcile] No cleared transactions to reconcile.")
                else:
                    diff = ynab_balance - amazon_balance
                    print(f"[reconcile] ✗ Balances do NOT match! Difference: ${diff}")
                    print("[reconcile] Please review transactions manually before reconciling.")


if __name__ == "__main__":
    main()
