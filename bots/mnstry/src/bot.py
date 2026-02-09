import os
import json
import time
from typing import Any, Dict, Optional, Tuple, List
from pathlib import Path

import requests

# ----------------------------
# Config
# ----------------------------
STATE_FILE = str((Path(__file__).resolve().parent.parent / "data" / "state.json"))

MNSTRY_BASE = "https://mnstry.com"
MNSTRY_HOME = f"{MNSTRY_BASE}/"
MNSTRY_PRODUCTS_JSON = f"{MNSTRY_BASE}/products.json?limit=250"

REQUEST_TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)

TOP_N = 5  # Top 5 in message 1, next 5 in message 2 if available

# Set to True if you want a "Sale ended" message when discounts disappear
NOTIFY_SALE_END = False

# How many total attempts for transient errors
MAX_ATTEMPTS = 3
RETRY_SLEEP_SECONDS = 3

# ----------------------------
# State
# ----------------------------
def load_state() -> Dict[str, Any]:
    """
    sale_active: was beim letzten Lauf irgendein rabattiertes Produkt aktiv?
    last_signature: Signatur des Top-Deals (nur Diagnose)
    """
    if not os.path.exists(STATE_FILE):
        return {"sale_active": False, "last_signature": ""}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        # Normalize expected keys
        if "sale_active" not in state:
            state["sale_active"] = False
        if "last_signature" not in state:
            state["last_signature"] = ""
        return state
    except Exception:
        # Corrupt or partial file: reset
        return {"sale_active": False, "last_signature": ""}


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ----------------------------
# Telegram
# ----------------------------
def telegram_send(message: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "disable_web_page_preview": False}

    r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()


# ----------------------------
# HTTP helpers
# ----------------------------
def http_get_json(url: str) -> Optional[Dict[str, Any]]:
    headers = {"User-Agent": USER_AGENT}

    try:
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return None

    # Wenn blockiert / temporär down: still skippen (keine Fehlalarme)
    if r.status_code != 200:
        return None

    # Shopify JSON sollte JSON sein; falls HTML kommt (WAF/Block), skip
    ct = (r.headers.get("content-type") or "").lower()
    if "application/json" not in ct and "json" not in ct:
        return None

    try:
        return r.json()
    except Exception:
        return None


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except Exception:
        return None


# ----------------------------
# Discount detection
# ----------------------------
def calc_discount(compare_at: float, price: float) -> Tuple[float, float]:
    """
    Returns: (discount_abs, discount_pct)
    """
    discount_abs = compare_at - price
    if compare_at <= 0:
        return discount_abs, 0.0
    discount_pct = (discount_abs / compare_at) * 100.0
    return discount_abs, discount_pct


def collect_deals(products: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int, int]:
    """
    Returns:
      deals: list of discounted variants with computed discount
      discounted_products_count: Produkte mit mind. 1 rabattierter Variante
      discounted_variants_count: Anzahl rabattierter Varianten
    """
    deals: List[Dict[str, Any]] = []
    discounted_products_count = 0
    discounted_variants_count = 0

    seen_variant_ids = set()

    for p in products:
        title = p.get("title") or "MNSTRY Product"
        handle = p.get("handle") or ""
        url = f"{MNSTRY_BASE}/products/{handle}" if handle else MNSTRY_HOME

        product_has_discount = False

        for v in p.get("variants", []) or []:
            price = to_float(v.get("price"))
            cap = to_float(v.get("compare_at_price"))
            variant_id = v.get("id")

            if price is None or cap is None:
                continue

            # Dedupe
            vid_int = int(variant_id) if variant_id is not None else 0
            if vid_int in seen_variant_ids:
                continue
            seen_variant_ids.add(vid_int)

            if cap > price:
                product_has_discount = True
                discounted_variants_count += 1

                disc_abs, disc_pct = calc_discount(cap, price)

                deals.append({
                    "title": title,
                    "url": url,
                    "variant_id": vid_int,
                    "price": price,
                    "compare_at": cap,
                    "discount_abs": disc_abs,
                    "discount_pct": disc_pct,
                })

        if product_has_discount:
            discounted_products_count += 1

    return deals, discounted_products_count, discounted_variants_count


def rank_deals(deals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Ranking:
      1) Rabatt % desc
      2) Rabatt CHF desc
      3) Preis asc (günstiger bevorzugt)
      4) variant_id (stable)
    """
    return sorted(
        deals,
        key=lambda d: (-d["discount_pct"], -d["discount_abs"], d["price"], d["variant_id"])
    )


def format_deal_line(d: Dict[str, Any]) -> str:
    return (
        f"• {d['title']}\n"
        f"  {d['compare_at']:.2f} → {d['price']:.2f}  "
        f"(-{d['discount_abs']:.2f} / {d['discount_pct']:.1f}%)\n"
        f"  {d['url']}"
    )


# ----------------------------
# Main
# ----------------------------
def run_once() -> None:
    state = load_state()

    data = http_get_json(MNSTRY_PRODUCTS_JSON)
    if not data:
        # keine Meldung, Status nicht kaputt machen
        return

    products = data.get("products", []) or []
    deals, discounted_products, discounted_variants = collect_deals(products)

    sale_now = len(deals) > 0
    ranked = rank_deals(deals)

    top = ranked[:TOP_N]
    next_top = ranked[TOP_N:TOP_N * 2]

    signature = ""
    if top:
        d0 = top[0]
        signature = f"{d0.get('variant_id',0)}|{d0['compare_at']:.2f}>{d0['price']:.2f}"

    was_active = bool(state.get("sale_active", False))

    # Meldung nur beim Wechsel: False -> True
    if (not was_active) and sale_now:
        # Message 1: Summary + Top N
        header_1 = (
            "🚨 MNSTRY Rabattaktion erkannt!\n\n"
            f"📦 Reduzierte Produkte: {discounted_products}\n"
            f"🏷️ Reduzierte Varianten: {discounted_variants}\n"
            f"🔗 {MNSTRY_HOME}\n\n"
            f"🔥 Top {min(TOP_N, len(top)) if top else TOP_N} Deals:\n"
        )
        body_1 = "\n\n".join(format_deal_line(d) for d in top) if top else "• (keine Details verfügbar)"
        telegram_send(header_1 + body_1)

        # Message 2: Always send (as requested)
        remaining_variants = max(0, discounted_variants - len(top))
        header_2 = (
            "📩 Weitere Infos:\n"
            f"• Weitere reduzierte Varianten (nach Top {len(top)}): {remaining_variants}\n"
        )

        if next_top:
            header_2 += "\n➡️ Nächste Top Deals:\n"
            body_2 = "\n\n".join(format_deal_line(d) for d in next_top)
            telegram_send(header_2 + body_2)
        else:
            header_2 += "\n(Keine weiteren Deals in den nächsten Slots.)"
            telegram_send(header_2)

    # Optional: Notify if sale ends
    if was_active and (not sale_now) and NOTIFY_SALE_END:
        telegram_send("✅ MNSTRY: Rabattaktion scheint beendet (keine reduzierten Varianten mehr gefunden).")

    # Update state
    state["sale_active"] = sale_now
    state["last_signature"] = signature
    save_state(state)


def main() -> None:
    # Retry bei kurzen Netzwerk-Hickups (transient)
    for attempt in range(MAX_ATTEMPTS):
        try:
            run_once()
            return
        except requests.RequestException:
            # transient network/http
            if attempt == MAX_ATTEMPTS - 1:
                raise
            time.sleep(RETRY_SLEEP_SECONDS * (attempt + 1))
        except Exception:
            # unknown exception: retry once or twice, then fail so we see it in Actions
            if attempt == MAX_ATTEMPTS - 1:
                raise
            time.sleep(RETRY_SLEEP_SECONDS)


if __name__ == "__main__":
    main()