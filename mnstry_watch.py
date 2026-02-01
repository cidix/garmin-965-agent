import os, json, re, time
import requests
from bs4 import BeautifulSoup

STATE_FILE = "mnstry_state.json"
UA = {"User-Agent": "Mozilla/5.0 Chrome/120"}

MNSTRY_HOME = "https://mnstry.com/"
MNSTRY_PRODUCTS_JSON = "https://mnstry.com/products.json?limit=250"

KEYWORDS = [
    "sale", "rabatt", "aktion", "angebot", "deals", "discount",
    "%", "code", "gutschein", "black friday", "cyber", "summer sale",
    "spar", "spare", "save"
]

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"sale_active": False, "last_signal": ""}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

def telegram_send(msg: str):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": chat_id, "text": msg, "disable_web_page_preview": False},
        timeout=25
    )
    r.raise_for_status()

def has_keyword_signal(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in KEYWORDS)

def check_homepage_signal() -> str:
    html = requests.get(MNSTRY_HOME, headers=UA, timeout=25).text
    soup = BeautifulSoup(html, "lxml")

    title = (soup.title.get_text(" ", strip=True) if soup.title else "")
    body_text = soup.get_text(" ", strip=True)
    body_text = re.sub(r"\s+", " ", body_text)[:3000]

    signal_text = f"{title} {body_text}"
    if has_keyword_signal(signal_text):
        return "homepage_keyword"
    return ""

def check_shopify_discount_signal() -> str:
    r = requests.get(MNSTRY_PRODUCTS_JSON, headers=UA, timeout=25)
    if r.status_code != 200:
        return ""
    data = r.json()

    products = data.get("products", [])
    for p in products:
        for v in p.get("variants", []):
            price = v.get("price")
            cap = v.get("compare_at_price")
            try:
                if price is None or cap is None:
                    continue
                price_f = float(price)
                cap_f = float(cap)
                if cap_f > price_f:
                    return "compare_at_price"
            except Exception:
                continue
    return ""

def main():
    state = load_state()

    s1 = check_homepage_signal()
    s2 = check_shopify_discount_signal()

    sale_now = bool(s1 or s2)
    signal = s2 or s1

    # Meldung nur beim Wechsel: kein Sale -> Sale
    if (not state["sale_active"]) and sale_now:
        telegram_send(
            "🚨 MNSTRY Rabattaktion erkannt!\n"
            f"Signal: {signal}\n"
            f"{MNSTRY_HOME}"
        )

    # Wenn Sale endet: wieder „scharf“ schalten, aber ohne Meldung
    state["sale_active"] = sale_now
    state["last_signal"] = signal
    save_state(state)

if __name__ == "__main__":
    for i in range(3):
        try:
            main()
            break
        except Exception:
            if i == 2:
                raise
            time.sleep(3)

