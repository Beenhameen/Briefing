import warnings
warnings.filterwarnings("ignore")

import yfinance as yf
import feedparser
import webbrowser
import json
import re
import math
import os
import hashlib
import http.server
import socketserver
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.request import urlopen
from playwright.sync_api import sync_playwright
from homeharvest import scrape_property

# --- DISMISSED PROPERTIES ---
_LIFE_DIR = os.path.dirname(os.path.abspath(__file__))
DISMISSED_PATH = os.path.join(_LIFE_DIR, 'dismissed_properties.json')
BRIEFING_PORT = 8765

def load_dismissed():
    try:
        with open(DISMISSED_PATH) as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_dismissed(dismissed_set):
    with open(DISMISSED_PATH, 'w') as f:
        json.dump(sorted(dismissed_set), f, indent=2)

def prop_id(link):
    return hashlib.md5(link.encode()).hexdigest()[:16]

TODAY = datetime.now()
MONTH = TODAY.month
DAY = TODAY.day

# --- STOCKS ---
TICKERS = ["SPY", "QQQ", "BTC-USD", "GLD", "DX-Y.NYB", "^VIX", "GOOGL", "NVDA", "COST"]

def get_stocks():
    results = []
    for symbol in TICKERS:
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="1y")
            if len(hist) < 2:
                continue
            closes = hist["Close"].tolist()
            prev = closes[-2]
            curr = closes[-1]
            change = ((curr - prev) / prev) * 100
            month_closes = closes[-21:] if len(closes) >= 21 else closes
            month_change = ((curr - month_closes[0]) / month_closes[0]) * 100
            results.append({
                "symbol": symbol,
                "price": round(curr, 2),
                "change": round(change, 2),
                "month_change": round(month_change, 2),
                "spark": [round(p, 2) for p in closes]
            })
        except:
            pass
    return results

# --- NEWS ---
RSS_FEEDS = [
    ("Reuters", "https://feeds.reuters.com/reuters/topNews"),
    ("BBC", "http://feeds.bbci.co.uk/news/world/rss.xml"),
    ("AP News", "https://rsshub.app/apnews/topics/apf-topnews"),
]

def get_news():
    articles = []
    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:4]:
                articles.append({
                    "source": source,
                    "title": entry.title,
                    "link": entry.link
                })
        except:
            pass
    return articles[:15]

# --- ON THIS DAY ---
def get_history():
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{MONTH}/{DAY}"
        with urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        events = data.get("events", [])[:5]
        return [{"year": e["year"], "text": e["text"]} for e in events]
    except:
        return []

# --- MARKET CONTEXT ---
def build_context(stocks):
    by_symbol = {s["symbol"]: s for s in stocks}

    lines = []

    vix = by_symbol.get("^VIX")
    if vix:
        v = vix["price"]
        if v >= 30:
            lines.append(f"VIX is at {v} — fear is elevated and volatility is spiking. Markets are in a risk-off mood.")
        elif v >= 20:
            lines.append(f"VIX is at {v} — above the calm threshold. Investors are uneasy; expect choppiness.")
        else:
            lines.append(f"VIX is at {v} — markets are calm and complacent. No widespread panic in the options market.")

    spy = by_symbol.get("SPY")
    qqq = by_symbol.get("QQQ")
    if spy and qqq:
        diff = round(qqq["change"] - spy["change"], 2)
        if spy["change"] > 0 and qqq["change"] > 0:
            if abs(diff) < 0.3:
                lines.append(f"Both SPY ({spy['change']:+.2f}%) and QQQ ({qqq['change']:+.2f}%) are up in lockstep — broad market rally with no obvious sector skew.")
            elif qqq["change"] > spy["change"]:
                lines.append(f"Tech is leading today — QQQ ({qqq['change']:+.2f}%) is outpacing SPY ({spy['change']:+.2f}%). Growth names are in favor.")
            else:
                lines.append(f"The broader market (SPY {spy['change']:+.2f}%) is outpacing tech (QQQ {qqq['change']:+.2f}%) — rotation into value or defensives.")
        elif spy["change"] < 0 and qqq["change"] < 0:
            lines.append(f"Both SPY ({spy['change']:+.2f}%) and QQQ ({qqq['change']:+.2f}%) are down — broad selloff today.")
        else:
            lines.append(f"SPY and QQQ are diverging (SPY {spy['change']:+.2f}%, QQQ {qqq['change']:+.2f}%) — mixed signals between tech and the wider market.")

    dxy = by_symbol.get("DX-Y.NYB")
    gld = by_symbol.get("GLD")
    btc = by_symbol.get("BTC-USD")
    if dxy:
        if dxy["change"] > 0.3:
            dollar_note = f"The dollar is strengthening ({dxy['change']:+.2f}%)"
            if gld and gld["change"] < 0:
                dollar_note += f", which is pressuring gold ({gld['change']:+.2f}%)"
            if btc and btc["change"] < 0:
                dollar_note += f" and crypto ({btc['change']:+.2f}%)"
            lines.append(dollar_note + ".")
        elif dxy["change"] < -0.3:
            dollar_note = f"The dollar is weakening ({dxy['change']:+.2f}%)"
            if gld and gld["change"] > 0:
                dollar_note += f", giving gold a lift ({gld['change']:+.2f}%)"
            if btc and btc["change"] > 0:
                dollar_note += f" and supporting crypto ({btc['change']:+.2f}%)"
            lines.append(dollar_note + ".")

    movers = [(s["symbol"], s["change"]) for s in stocks if s["symbol"] not in ("SPY","QQQ","^VIX","DXY") and abs(s["change"]) >= 2]
    movers.sort(key=lambda x: abs(x[1]), reverse=True)
    if movers:
        mover_text = ", ".join(f"{sym} {chg:+.2f}%" for sym, chg in movers[:3])
        lines.append(f"Notable movers today: {mover_text}.")

    return " ".join(lines) if lines else "Market data unavailable for context."


# --- EV MARKET ---
EV_TICKERS = ["TSLA", "RIVN", "LCID", "NIO", "F", "GM"]

def get_ev_stocks():
    results = []
    for symbol in EV_TICKERS:
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="2d")
            if len(hist) < 2:
                continue
            prev = hist["Close"].iloc[-2]
            curr = hist["Close"].iloc[-1]
            change = ((curr - prev) / prev) * 100
            results.append({"symbol": symbol, "price": round(curr, 2), "change": round(change, 2)})
        except:
            pass
    return results

def build_ev_context(ev_stocks):
    if not ev_stocks:
        return None, "EV market data unavailable today."

    avg_change = sum(s["change"] for s in ev_stocks) / len(ev_stocks)
    by_symbol = {s["symbol"]: s for s in ev_stocks}

    # Sentiment
    if avg_change <= -2:
        sentiment = "bearish"
        sentiment_note = f"EV stocks are getting hit hard today (avg {avg_change:+.1f}%). Manufacturers under pressure may push incentives and discounts to move inventory."
    elif avg_change < 0:
        sentiment = "soft"
        sentiment_note = f"EV stocks are drifting lower (avg {avg_change:+.1f}%). Mild weakness — dealers may have room to negotiate."
    elif avg_change < 1.5:
        sentiment = "neutral"
        sentiment_note = f"EV stocks are relatively flat today (avg {avg_change:+.1f}%). No strong signals either way."
    else:
        sentiment = "bullish"
        sentiment_note = f"EV stocks are up today (avg {avg_change:+.1f}%). Demand looks healthy — expect less dealer flexibility on price."

    # Notable movers
    movers = sorted(ev_stocks, key=lambda x: abs(x["change"]), reverse=True)[:2]
    mover_text = " | ".join(f"{s['symbol']} {s['change']:+.2f}%" for s in movers)

    # Buy signal
    if sentiment in ("bearish", "soft"):
        verdict = "FAVORABLE"
        verdict_color = "#10b981"
        verdict_note = "Stock weakness typically translates to on-the-lot discounts and increased manufacturer incentives. Good window to negotiate."
    elif sentiment == "neutral":
        verdict = "NEUTRAL"
        verdict_color = "#f59e0b"
        verdict_note = "No strong signal. Standard market conditions — do your homework on incentives before buying."
    else:
        verdict = "WAIT"
        verdict_color = "#ef4444"
        verdict_note = "EV demand is strong right now. Manufacturers have less incentive to discount. Consider waiting for a softer stretch."

    # Average price reference (updated periodically — source: Cox Automotive / Kelley Blue Book)
    avg_price_note = "Average new EV transaction price: ~$52,000 (per KBB Q1 2026). Prices have been declining ~8% year-over-year as competition increases."

    summary = f"{sentiment_note} {avg_price_note}"
    return {"verdict": verdict, "verdict_color": verdict_color, "verdict_note": verdict_note, "summary": summary, "movers": mover_text}, None


# --- SPARKLINE ---
def make_sparkline(prices, color, height=48):
    if not prices or len(prices) < 2:
        return ""
    n = len(prices)
    lo = min(prices)
    hi = max(prices)
    spread = hi - lo or 1
    pad_y = 3
    vw, vh = 100, height
    xs = [(i / (n - 1)) * vw for i in range(n)]
    ys = [pad_y + (1 - (p - lo) / spread) * (vh - 2 * pad_y) for p in prices]
    points = " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, ys))
    return (
        f'<svg width="100%" height="{height}" viewBox="0 0 {vw} {vh}" '
        f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
        f'<polyline points="{points}" fill="none" stroke="{color}" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        f'</svg>'
    )

# --- MORTGAGE ---
def get_mortgage_rate():
    try:
        url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=MORTGAGE30US"
        with urlopen(url, timeout=8) as r:
            lines = r.read().decode().strip().split("\n")
        for line in reversed(lines[1:]):
            parts = line.split(",")
            if len(parts) == 2 and parts[1].strip() != ".":
                return float(parts[1].strip())
    except:
        pass
    return 6.74  # fallback: ~30-year fixed as of early 2026

def monthly_payment(price, down, annual_rate, years=30):
    principal = price - down
    if principal <= 0:
        return 0
    r = annual_rate / 100 / 12
    n = years * 12
    return round(principal * r * (1 + r)**n / ((1 + r)**n - 1))

# --- BUILD HTML ---
# --- EQUIPMENT DEALS ---
# Cultus property: CTL for blackberry knockdown / land clearing, PNW terrain
CTL_KEYWORDS = [
    "compact track loader", "track loader", "ctl",
    "259d", "259d3", "svl75", "svl 75", "t66", "t64",
    "rt-65", "rt65", "3ts-8t", "teleskid",
]
ATTACHMENT_KEYWORDS = [
    "attachment", "bucket", "auger", "grapple", "blade", "broom", "sweeper",
    "mulcher", "mower", "breaker", "hammer", "forks", "fork", "coupler",
    "quick attach", "thumb", " part ", "parts", "hydraulic valve", "valve",
    "cutting edge", "teeth", "tooth", "seat", "door", "canopy", "cab kit",
    "tire", "undercarriage", "manual", "joystick", "pump", "cylinder",
    "compactor plate", "drum compactor", "plate compactor", "for skid steer",
    "for excavator", "for mini", "fits ", "fits:"
]
MACHINE_BRANDS = [
    "bobcat", "cat ", "caterpillar", "kubota", "asv", "jcb",
    "case ", "new holland", "gehl", "mustang", "takeuchi",
]
CL_SEARCHES = [
    ("seattle",   "compact track loader"),
    ("seattle",   "track loader"),
    ("tacoma",    "compact track loader"),
    ("portland",  "compact track loader"),
    ("spokane",   "compact track loader"),
    ("olympia",   "compact track loader"),
]
AVG_PRICES = {"Compact Track Loaders": 42000}

def is_full_machine(title):
    t = title.lower()
    if any(k in t for k in ATTACHMENT_KEYWORDS):
        return False
    has_year = bool(re.search(r'\b(19|20)\d{2}\b', title))
    has_brand = any(b in t for b in MACHINE_BRANDS)
    return has_year or has_brand

def classify(title):
    t = title.lower()
    if any(k in t for k in CTL_KEYWORDS):
        return "Compact Track Loaders"
    return None

def _add_ctl(results, seen, title, price, link, source):
    if not link or link in seen:
        return
    seen.add(link)
    cat = classify(title) or "Compact Track Loaders"
    avg = AVG_PRICES[cat]
    discount = round((avg - price) / avg * 100, 1)
    results[cat].append({"title": title, "price": price, "avg": avg,
                          "discount": discount, "link": link, "source": source})

def get_equipment_deals():
    results = {"Compact Track Loaders": []}
    seen = set()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
            page = ctx.new_page()

            # ── Craigslist ──
            for city, query in CL_SEARCHES:
                try:
                    url = f"https://{city}.craigslist.org/search/hvo?query={query.replace(' ', '+')}"
                    page.goto(url, timeout=15000)
                    page.wait_for_load_state("networkidle", timeout=15000)
                    for card in page.query_selector_all("[data-pid]"):
                        try:
                            link_el = card.query_selector("a")
                            if not link_el:
                                continue
                            link = link_el.get_attribute("href")
                            text = card.inner_text()
                            pm = re.search(r'\$([\d,]+)', text)
                            if not pm:
                                continue
                            price = int(pm.group(1).replace(",", ""))
                            if price < 20000 or price > 85000:
                                continue
                            lines = [l.strip() for l in text.split("\n") if l.strip() and l.strip() != "•"]
                            title = next((l for l in lines if len(l) > 8 and "$" not in l), lines[0] if lines else "")
                            if not is_full_machine(title):
                                continue
                            _add_ctl(results, seen, title, price, link, f"{city.capitalize()} · Craigslist")
                        except:
                            pass
                except:
                    pass

            # ── MachineryTrader ──
            for state in ["WA", "OR"]:
                try:
                    url = (f"https://www.machinerytrader.com/listings/construction/for-sale/"
                           f"compact-track-loaders/list/?State={state}&priceMin=20000&priceMax=85000")
                    page.goto(url, timeout=20000)
                    page.wait_for_load_state("networkidle", timeout=20000)
                    cards = page.query_selector_all("article, .item-details, [class*='listing-card'], li.result")
                    for card in cards:
                        try:
                            text = card.inner_text()
                            pm = re.search(r'\$\s*([\d,]+)', text)
                            if not pm:
                                continue
                            price = int(pm.group(1).replace(",", ""))
                            if price < 20000 or price > 85000:
                                continue
                            link_el = card.query_selector("a")
                            if not link_el:
                                continue
                            link = link_el.get_attribute("href") or ""
                            if link and not link.startswith("http"):
                                link = "https://www.machinerytrader.com" + link
                            lines = [l.strip() for l in text.split("\n") if l.strip() and "$" not in l]
                            title = next((l for l in lines if len(l) > 8), lines[0] if lines else "")
                            if not is_full_machine(title):
                                continue
                            _add_ctl(results, seen, title, price, link, f"MachineryTrader · {state}")
                        except:
                            pass
                except:
                    pass

            # ── EquipmentTrader ──
            for state in ["WA", "OR"]:
                try:
                    url = (f"https://www.equipmenttrader.com/compact-track-loaders-for-sale/"
                           f"?state%5B%5D={state}&price%5Bmin%5D=20000&price%5Bmax%5D=85000")
                    page.goto(url, timeout=20000)
                    page.wait_for_load_state("networkidle", timeout=20000)
                    cards = page.query_selector_all("[data-cmp='searchListing'], article, .listing-item, [class*='listing']")
                    for card in cards:
                        try:
                            text = card.inner_text()
                            pm = re.search(r'\$\s*([\d,]+)', text)
                            if not pm:
                                continue
                            price = int(pm.group(1).replace(",", ""))
                            if price < 20000 or price > 85000:
                                continue
                            link_el = card.query_selector("a")
                            if not link_el:
                                continue
                            link = link_el.get_attribute("href") or ""
                            if link and not link.startswith("http"):
                                link = "https://www.equipmenttrader.com" + link
                            lines = [l.strip() for l in text.split("\n") if l.strip() and "$" not in l]
                            title = next((l for l in lines if len(l) > 8), lines[0] if lines else "")
                            if not is_full_machine(title):
                                continue
                            _add_ctl(results, seen, title, price, link, f"EquipmentTrader · {state}")
                        except:
                            pass
                except:
                    pass

            # ── IronPlanet ──
            try:
                url = "https://www.ironplanet.com/results?category=Compact+Track+Loaders&stateProv=WA,OR,ID"
                page.goto(url, timeout=20000)
                page.wait_for_load_state("networkidle", timeout=20000)
                page.wait_for_timeout(3000)
                cards = page.query_selector_all(".lot-card, [class*='lot-item'], [class*='LotCard'], article")
                for card in cards:
                    try:
                        text = card.inner_text()
                        pm = re.search(r'\$\s*([\d,]+)', text)
                        if not pm:
                            continue
                        price = int(pm.group(1).replace(",", ""))
                        if price < 20000 or price > 85000:
                            continue
                        link_el = card.query_selector("a")
                        if not link_el:
                            continue
                        link = link_el.get_attribute("href") or ""
                        if link and not link.startswith("http"):
                            link = "https://www.ironplanet.com" + link
                        lines = [l.strip() for l in text.split("\n") if l.strip() and "$" not in l]
                        title = next((l for l in lines if len(l) > 8), lines[0] if lines else "")
                        _add_ctl(results, seen, title, price, link, "IronPlanet · Auction")
                    except:
                        pass
            except:
                pass

            # ── Ritchie Bros ──
            try:
                url = "https://www.rbauction.com/equipment?equipmentType=compact-track-loaders&state=WA,OR,ID"
                page.goto(url, timeout=20000)
                page.wait_for_load_state("networkidle", timeout=20000)
                page.wait_for_timeout(3000)
                cards = page.query_selector_all("article, .lot-card, [class*='result-item'], [class*='listing']")
                for card in cards:
                    try:
                        text = card.inner_text()
                        pm = re.search(r'\$\s*([\d,]+)', text)
                        if not pm:
                            continue
                        price = int(pm.group(1).replace(",", ""))
                        if price < 20000 or price > 85000:
                            continue
                        link_el = card.query_selector("a")
                        if not link_el:
                            continue
                        link = link_el.get_attribute("href") or ""
                        if link and not link.startswith("http"):
                            link = "https://www.rbauction.com" + link
                        lines = [l.strip() for l in text.split("\n") if l.strip() and "$" not in l]
                        title = next((l for l in lines if len(l) > 8), lines[0] if lines else "")
                        _add_ctl(results, seen, title, price, link, "Ritchie Bros · Auction")
                    except:
                        pass
            except:
                pass

            # ── Purple Wave ──
            try:
                url = "https://www.purplewave.com/auction/search/?q=compact+track+loader"
                page.goto(url, timeout=20000)
                page.wait_for_load_state("networkidle", timeout=20000)
                cards = page.query_selector_all("article, .auction-item, [class*='item-card'], [class*='lot']")
                for card in cards:
                    try:
                        text = card.inner_text()
                        pm = re.search(r'\$\s*([\d,]+)', text)
                        if not pm:
                            continue
                        price = int(pm.group(1).replace(",", ""))
                        if price < 20000 or price > 85000:
                            continue
                        link_el = card.query_selector("a")
                        if not link_el:
                            continue
                        link = link_el.get_attribute("href") or ""
                        if link and not link.startswith("http"):
                            link = "https://www.purplewave.com" + link
                        lines = [l.strip() for l in text.split("\n") if l.strip() and "$" not in l]
                        title = next((l for l in lines if len(l) > 8), lines[0] if lines else "")
                        _add_ctl(results, seen, title, price, link, "Purple Wave · Auction")
                    except:
                        pass
            except:
                pass

            browser.close()
    except Exception as e:
        print(f"Equipment scrape error: {e}")

    for cat in results:
        results[cat].sort(key=lambda x: x["discount"], reverse=True)
        results[cat] = results[cat][:8]
    return results


# --- PHEV DEALS ---
# Your current vehicle
CURRENT_CAR_MPG   = 24      # 2009 Honda CR-V combined MPG
CURRENT_CAR_TRADE = 4500    # KBB fair condition, 160k mi
FUEL_PRICE_GAL    = 3.50    # $/gallon
ANNUAL_MILES      = 15000
LONG_TRIPS_MO     = 2       # long trips per month
LONG_TRIP_MI      = 300     # round-trip miles per long trip
# Electricity cost = $0 (solar)

PHEV_SPECS = {
    'RAV4 Prime':     {'ev': 42, 'mpg': 38, 'life': 250000, 'avg': 43000},
    'Escape PHEV':    {'ev': 37, 'mpg': 40, 'life': 200000, 'avg': 30000},
    'Tucson PHEV':    {'ev': 33, 'mpg': 35, 'life': 200000, 'avg': 33000},
    'Outlander PHEV': {'ev': 38, 'mpg': 30, 'life': 200000, 'avg': 30000},
}

PHEV_CL_SEARCHES = [
    ('seattle',  'RAV4 Prime'),
    ('tacoma',   'RAV4 Prime'),
    ('portland', 'RAV4 Prime'),
    ('spokane',  'RAV4 Prime'),
    ('seattle',  'Escape PHEV'),
    ('portland', 'Escape PHEV'),
    ('seattle',  'Outlander PHEV'),
    ('portland', 'Tucson PHEV'),
]

def _detect_phev_model(text):
    t = text.lower()
    if 'rav4' in t and 'prime' in t:     return 'RAV4 Prime'
    if 'escape' in t and 'phev' in t:    return 'Escape PHEV'
    if 'tucson' in t and 'phev' in t:    return 'Tucson PHEV'
    if 'outlander' in t and 'phev' in t: return 'Outlander PHEV'
    return None

def phev_roi(price, odometer, model_name):
    spec = PHEV_SPECS.get(model_name, PHEV_SPECS['RAV4 Prime'])
    odo = odometer or 0
    remaining = max(0, spec['life'] - odo) if odometer is not None else None
    cpm = round(price / remaining, 3) if remaining else None
    net_cost = price - CURRENT_CAR_TRADE

    current_fuel_yr = (ANNUAL_MILES / CURRENT_CAR_MPG) * FUEL_PRICE_GAL

    # Solar = free electricity. Gas only on long trips beyond EV range.
    # Each 300-mi round trip: 2 × ev_range miles electric, rest gas.
    gas_mi_per_trip = max(0, LONG_TRIP_MI - 2 * spec['ev'])
    gas_mi_yr = LONG_TRIPS_MO * 12 * gas_mi_per_trip
    phev_fuel_yr = (gas_mi_yr / spec['mpg']) * FUEL_PRICE_GAL

    savings_yr = current_fuel_yr - phev_fuel_yr
    payback = (net_cost / savings_yr) if savings_yr > 0 else None

    return {
        'remaining':  remaining,
        'cpm':        cpm,
        'net_cost':   net_cost,
        'fuel_yr':    round(phev_fuel_yr),
        'savings_yr': round(savings_yr),
        'payback_yr': round(payback, 1) if payback else None,
        'gain_5yr':   round(savings_yr * 5 - net_cost),
        'gain_10yr':  round(savings_yr * 10 - net_cost),
    }

def _extract_odo(text):
    m = re.search(r'\b(\d{1,3},\d{3})\s*(?:miles?|mi)\b', text, re.I)
    if m: return int(m.group(1).replace(',', ''))
    m2 = re.search(r'\b(\d+)k\s*(?:miles?|mi)\b', text, re.I)
    if m2: return int(m2.group(1)) * 1000
    return None

def get_phev_deals():
    results = []
    seen = set()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()

            # ── Craigslist ──
            for city, query in PHEV_CL_SEARCHES:
                try:
                    url = f"https://{city}.craigslist.org/search/cto?query={query.replace(' ', '+')}"
                    page.goto(url, timeout=15000)
                    page.wait_for_load_state("networkidle", timeout=15000)
                    for card in page.query_selector_all("[data-pid]"):
                        try:
                            link_el = card.query_selector("a")
                            if not link_el: continue
                            link = link_el.get_attribute("href")
                            if not link or link in seen: continue
                            seen.add(link)
                            text = card.inner_text()
                            pm = re.search(r'\$([\d,]+)', text)
                            if not pm: continue
                            price = int(pm.group(1).replace(',', ''))
                            if price < 15000 or price > 80000: continue
                            lines = [l.strip() for l in text.split('\n') if l.strip() and l.strip() != '•']
                            title = next((l for l in lines if len(l) > 8 and '$' not in l), lines[0] if lines else '')
                            model = _detect_phev_model(title) or _detect_phev_model(query)
                            if not model: continue
                            odo = _extract_odo(text)
                            spec = PHEV_SPECS[model]
                            roi = phev_roi(price, odo, model)
                            results.append({
                                'title':    title,
                                'model':    model,
                                'price':    price,
                                'avg':      spec['avg'],
                                'discount': round((spec['avg'] - price) / spec['avg'] * 100, 1),
                                'link':     link,
                                'source':   f"{city.capitalize()} · Craigslist",
                                'odometer': odo,
                                'roi':      roi,
                                'salvage':  False,
                            })
                        except: pass
                except: pass

            # ── Copart (salvage auction) ──
            for query in ['RAV4 Prime', 'Escape PHEV', 'Outlander PHEV']:
                try:
                    url = f"https://www.copart.com/public/lots/search?query={query.replace(' ', '+')}"
                    page.goto(url, timeout=20000)
                    page.wait_for_timeout(4000)
                    cards = page.query_selector_all('.lot-card, [class*="lot-details"], [class*="LotCard"]')
                    for card in cards[:8]:
                        try:
                            text = card.inner_text()
                            if len(text) < 10: continue
                            pm = re.search(r'\$\s*([\d,]+)', text)
                            if not pm: continue
                            price = int(pm.group(1).replace(',', ''))
                            if price < 3000 or price > 55000: continue
                            link_el = card.query_selector('a')
                            href = link_el.get_attribute('href') if link_el else ''
                            if href and not href.startswith('http'):
                                href = 'https://www.copart.com' + href
                            key = href or text[:60]
                            if key in seen: continue
                            seen.add(key)
                            lines = [l.strip() for l in text.split('\n') if l.strip()]
                            title = lines[0] if lines else query
                            model = _detect_phev_model(title) or _detect_phev_model(query)
                            if not model: continue
                            odo = _extract_odo(text)
                            spec = PHEV_SPECS[model]
                            roi = phev_roi(price, odo, model)
                            results.append({
                                'title':    f'[SALVAGE] {title}',
                                'model':    model,
                                'price':    price,
                                'avg':      spec['avg'],
                                'discount': round((spec['avg'] - price) / spec['avg'] * 100, 1),
                                'link':     href or url,
                                'source':   'Copart · Salvage Auction',
                                'odometer': odo,
                                'roi':      roi,
                                'salvage':  True,
                            })
                        except: pass
                except: pass

            browser.close()
    except Exception as e:
        print(f"PHEV scrape error: {e}")

    results.sort(key=lambda x: x['roi']['gain_5yr'], reverse=True)
    return results[:15]

# Keep alias so build_html signature doesn't need to change
def get_rav4_deals():
    return get_phev_deals()


# --- REAL ESTATE DEALS ---
# Averages for north/east Seattle suburbs (2026)
RE_AVG_PRICES = {"Residential": 850000, "Land / Acreage": 320000}

# Bethany Community Church NE — 3841 NE 123rd St, Seattle
CHURCH_COORDS = (47.719, -122.286)

def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return round(R * 2 * math.asin(math.sqrt(a)), 1)

RE_SEARCH_CITIES = [
    "Shoreline, WA", "Kenmore, WA", "Bothell, WA", "Woodinville, WA",
    "Kirkland, WA", "Redmond, WA", "Duvall, WA", "Snohomish, WA",
    "Edmonds, WA", "Monroe, WA", "Bellevue, WA", "Issaquah, WA",
    "Lake Forest Park, WA", "Lynnwood, WA",
]

# Craigslist FSBO — approximate coords per neighborhood for distance estimation
CL_AREA_COORDS = {
    "shoreline": (47.7561, -122.3415), "kenmore": (47.7573, -122.2440),
    "bothell": (47.7623, -122.2054), "woodinville": (47.7540, -122.1638),
    "mill creek": (47.8607, -122.2041), "lynnwood": (47.8209, -122.3151),
    "mountlake terrace": (47.7887, -122.3076), "kirkland": (47.6769, -122.2060),
    "redmond": (47.6740, -122.1215), "sammamish": (47.6163, -122.0356),
    "bellevue": (47.6101, -122.2015), "issaquah": (47.5301, -122.0326),
    "duvall": (47.7415, -121.9854), "carnation": (47.6479, -121.9126),
    "lake forest park": (47.7554, -122.2804), "maltby": (47.8068, -122.1340),
    "cottage lake": (47.7468, -122.0993), "novelty hill": (47.6943, -122.0527),
    "fall city": (47.5651, -121.8882), "snohomish": (47.9129, -122.0982),
    "north bend": (47.4957, -121.7868), "edmonds": (47.8107, -122.3776),
    "monroe": (47.8554, -121.9715), "lake stevens": (48.0126, -122.0643),
    "clearview": (47.8210, -122.0854), "north seattle": (47.7184, -122.3262),
    "lake city": (47.7184, -122.2862), "wedgwood": (47.7068, -122.2884),
    "sand point": (47.6840, -122.2637), "matthews beach": (47.7040, -122.2711),
}
CL_RE_VALID    = list(CL_AREA_COORDS.keys())
CL_RE_EXCLUDE  = [
    "for rent", "for lease", "rental", "lease", "timeshare", "time share",
    "room for", "room rent", "wanted", "looking for", "seeking", "condo",
]
CL_RE_SEARCHES = [
    ("seattle", "reo", "acres house"),
    ("seattle", "reo", "acres land"),
    ("seattle", "reo", "acreage"),
]

def get_craigslist_re():
    items = []
    seen = set()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            for city, section, query in CL_RE_SEARCHES:
                try:
                    url = f"https://{city}.craigslist.org/search/{section}?query={query.replace(' ', '+')}"
                    page.goto(url, timeout=15000)
                    page.wait_for_load_state("networkidle", timeout=15000)
                    for card in page.query_selector_all("[data-pid]"):
                        try:
                            link_el = card.query_selector("a")
                            if not link_el:
                                continue
                            link = link_el.get_attribute("href")
                            if not link or link in seen:
                                continue
                            seen.add(link)
                            text = card.inner_text()
                            m = re.search(r'(\d+\.?\d*)\s*acres?', text, re.IGNORECASE)
                            if not m or float(m.group(1)) < 0.75:
                                continue
                            acres = float(m.group(1))
                            tl = text.lower()
                            if not any(loc in tl for loc in CL_RE_VALID):
                                continue
                            pm = re.search(r'\$([\d,]+)', text)
                            if not pm:
                                continue
                            price = int(pm.group(1).replace(",", ""))
                            if price < 50000 or price > 1500000:
                                continue
                            lines = [l.strip() for l in text.split("\n") if l.strip() and l.strip() != "•"]
                            title = next((l for l in lines if len(l) > 8 and "$" not in l), lines[0] if lines else "")
                            if any(k in title.lower() for k in CL_RE_EXCLUDE):
                                continue
                            cat = "Land / Acreage" if any(k in title.lower() for k in ["land", "lot", "parcel", "ranch", "farm", "acreage", "timber", "raw"]) else "Residential"
                            dist = next((haversine(*coords, *CHURCH_COORDS) for loc, coords in CL_AREA_COORDS.items() if loc in tl), None)
                            items.append({
                                "cat": cat,
                                "title": title,
                                "price": price,
                                "avg": RE_AVG_PRICES[cat],
                                "discount": round((RE_AVG_PRICES[cat] - price) / RE_AVG_PRICES[cat] * 100, 1),
                                "link": link,
                                "source": "Craigslist · FSBO",
                                "acres": round(acres, 2),
                                "distance": dist,
                                "photos": [],
                            })
                        except:
                            pass
                except:
                    pass
            browser.close()
    except Exception as e:
        print(f"Craigslist RE error: {e}")
    return items

def get_realestate_deals():
    def fetch(city):
        try:
            return scrape_property(
                location=city,
                listing_type="for_sale",
                lot_sqft_min=32670,
                price_min=50000,
                price_max=1500000,
                limit=100,
            )
        except:
            return None

    with ThreadPoolExecutor(max_workers=6) as ex:
        frames = [f for f in ex.map(fetch, RE_SEARCH_CITIES) if f is not None and not f.empty]

    results = {"Residential": [], "Land / Acreage": []}
    seen = set()

    for df in frames:
        for _, row in df.iterrows():
            try:
                uid = str(row.get("mls_id") or f"{row.get('street')}{row.get('city')}")
                if uid in seen:
                    continue
                seen.add(uid)

                price = row.get("list_price")
                lot_sqft = row.get("lot_sqft")
                if not price or not lot_sqft:
                    continue
                acres = lot_sqft / 43560
                if acres < 0.75:
                    continue

                style = str(row.get("style", "")).upper()
                cat = "Land / Acreage" if any(k in style for k in ("LAND", "FARM", "RANCH", "LOT")) else "Residential"

                title = f"{row.get('street', '')}, {row.get('city', '')}"
                url = str(row.get("property_url", "#"))
                avg = RE_AVG_PRICES[cat]
                discount = round((avg - int(price)) / avg * 100, 1)

                try:
                    lat_v = float(row["latitude"])
                    lon_v = float(row["longitude"])
                    dist = haversine(lat_v, lon_v, *CHURCH_COORDS)
                except:
                    lat_v = lon_v = dist = None

                alt = row.get("alt_photos")
                if alt and str(alt) not in ("nan", "None", ""):
                    photos = [u.strip() for u in str(alt).split(",") if u.strip().startswith("http")]
                elif row.get("primary_photo"):
                    photos = [str(row["primary_photo"])]
                else:
                    photos = []

                results[cat].append({
                    "title": title,
                    "price": int(price),
                    "avg": avg,
                    "discount": discount,
                    "link": url,
                    "source": "Realtor.com · MLS",
                    "acres": round(acres, 2),
                    "distance": dist,
                    "photos": photos,
                    "lat": lat_v,
                    "lon": lon_v,
                    "lot_sqft": int(lot_sqft),
                })
            except:
                pass

    cl_items = get_craigslist_re()
    print(f"Craigslist FSBO found: {len(cl_items)}")
    for item in cl_items:
        cat = item.pop("cat")
        results[cat].append(item)

    for cat in results:
        results[cat].sort(key=lambda x: x["distance"] if x["distance"] is not None else 999)
        results[cat] = results[cat][:10]
    return results


def build_html(stocks, news, history, ev_stocks, deals, rav4_deals, re_deals, mortgage_rate=6.74):
    date_str = TODAY.strftime("%A, %B %d %Y")
    context = build_context(stocks)
    ev_data, ev_error = build_ev_context(ev_stocks)
    if ev_data:
        ev_html = f"""
    <div class="ev-box">
      <div class="ev-verdict">
        <span class="ev-badge" style="color:{ev_data['verdict_color']}">{ev_data['verdict']}</span>
        <span class="ev-verdict-note">{ev_data['verdict_note']}</span>
      </div>
      <div class="ev-body">
        {ev_data['summary']}
        <div class="ev-movers">Today's EV stocks: {ev_data['movers']}</div>
      </div>
    </div>"""
    else:
        ev_html = f'<div class="context-box">{ev_error}</div>'

    stock_cards = ""
    for s in stocks:
        mc = s.get("month_change", s["change"])
        if mc > 0:
            cls = "up"
            arrow = "▲"
            top_color = "#10b981"
        elif mc < 0:
            cls = "down"
            arrow = "▼"
            top_color = "#ef4444"
        else:
            cls = "flat"
            arrow = "—"
            top_color = "#475569"
        spark_svg = make_sparkline(s.get("spark", []), top_color)
        spark_json = json.dumps(s.get("spark", []))
        display_symbol = "DXY" if s["symbol"] == "DX-Y.NYB" else s["symbol"]
        stock_cards += f"""
        <div class="stock-card" style="--card-color:{top_color}" data-spark='{spark_json}'>
            <div class="stock-ticker">{display_symbol}</div>
            <div class="stock-spark">{spark_svg}</div>
            <div class="stock-footer">
                <div class="stock-price">${s["price"]:,}</div>
                <div class="stock-change {cls}">{arrow} {abs(mc)}%</div>
            </div>
        </div>"""

    news_items = ""
    for a in news:
        news_items += f"""
        <a class="news-item" href="{a["link"]}" target="_blank">
            <span class="news-source">{a["source"]}</span>
            <span class="news-title">{a["title"]}</span>
        </a>"""

    history_items = ""
    for e in history:
        history_items += f"""
        <div class="history-item">
            <span class="history-year">{e["year"]}</span>
            <span class="history-text">{e["text"]}</span>
        </div>"""

    def render_deal_list(items):
        if not items:
            return '<div class="no-deals">No priced listings found today.</div>'
        cards = ""
        for d in items:
            color = "#10b981" if d["discount"] > 0 else "#ef4444" if d["discount"] < 0 else "#475569"
            label = f"-{d['discount']}%" if d["discount"] > 0 else f"+{abs(d['discount'])}%"
            cards += f"""
        <a class="deal-card" href="{d['link']}" target="_blank">
            <div class="deal-discount" style="color:{color}">{label}</div>
            <div class="deal-info">
                <div class="deal-title">{d['title']}</div>
                <div class="deal-meta">{d['source']} · avg used ~${d['avg']:,}</div>
            </div>
            <div class="deal-price">
                ${d['price']:,}
                <div class="deal-avg">${d['avg']:,}</div>
            </div>
        </a>"""
        return f'<div class="deal-list">{cards}</div>'

    deals_html = ""
    for cat, items in deals.items():
        deals_html += f'<div class="subsection-title">{cat}</div>{render_deal_list(items)}'

    def render_phev_cards(items):
        if not items:
            return '<div class="no-deals">No PHEV listings found today.</div>'
        current_fuel_yr = round((ANNUAL_MILES / CURRENT_CAR_MPG) * FUEL_PRICE_GAL)
        header = f"""<div class="phev-assumptions">
          <span><strong>Your CR-V:</strong> ~${current_fuel_yr:,}/yr fuel ({ANNUAL_MILES:,} mi @ {CURRENT_CAR_MPG} MPG, ${FUEL_PRICE_GAL}/gal)</span>
          <span><strong>Trade-in:</strong> ~${CURRENT_CAR_TRADE:,}</span>
          <span><strong>Electricity:</strong> $0 (solar)</span>
          <span><strong>Long trips:</strong> {LONG_TRIPS_MO}×/mo, {LONG_TRIP_MI} mi round-trip</span>
        </div>"""
        cards = ""
        for d in items:
            roi = d['roi']
            disc_color = "#10b981" if d['discount'] > 0 else "#ef4444"
            disc_str   = f"-{d['discount']}%" if d['discount'] > 0 else f"+{abs(d['discount'])}% over avg"
            odo_str    = f"{d['odometer']:,} mi" if d.get('odometer') else "unknown"
            rem_str    = f"{roi['remaining']:,}" if roi.get('remaining') else "—"
            cpm_str    = f"${roi['cpm']:.3f}" if roi.get('cpm') else "—"
            pay_str    = f"{roi['payback_yr']} yrs" if roi.get('payback_yr') else "—"
            g5c        = "#10b981" if roi['gain_5yr'] > 0 else "#ef4444"
            g10c       = "#10b981" if roi['gain_10yr'] > 0 else "#ef4444"
            g5_str     = f"{'+'if roi['gain_5yr']>=0 else ''}${roi['gain_5yr']:,}"
            g10_str    = f"{'+'if roi['gain_10yr']>=0 else ''}${roi['gain_10yr']:,}"
            salvage_tag = '<span class="salvage-tag">SALVAGE TITLE</span>' if d.get('salvage') else ''
            cards += f"""
        <div class="phev-card{' phev-salvage' if d.get('salvage') else ''}">
          <div class="phev-card-top">
            <div style="min-width:0">
              <a href="{d['link']}" target="_blank" class="phev-card-title">{d['title']}</a>
              <div class="phev-card-meta">
                <span class="phev-model-tag">{d['model']}</span>
                {d['source']} · {odo_str}
                {salvage_tag}
              </div>
            </div>
            <div class="phev-price-block">
              <div class="phev-price-num">${d['price']:,}</div>
              <div class="phev-price-vs" style="color:{disc_color}">{disc_str}</div>
            </div>
          </div>
          <div class="phev-metrics">
            <div class="phev-metric"><div class="phev-metric-label">Net After Trade-in</div><div class="phev-metric-val">${roi['net_cost']:,}</div></div>
            <div class="phev-metric"><div class="phev-metric-label">Rem. Miles Est.</div><div class="phev-metric-val">{rem_str}</div></div>
            <div class="phev-metric"><div class="phev-metric-label">$ / Rem. Mile</div><div class="phev-metric-val">{cpm_str}</div></div>
            <div class="phev-metric"><div class="phev-metric-label">Fuel Cost / yr</div><div class="phev-metric-val">${roi['fuel_yr']:,} <span style="font-size:.7rem;color:var(--muted)">(solar)</span></div></div>
            <div class="phev-metric"><div class="phev-metric-label">Annual Savings</div><div class="phev-metric-val" style="color:#10b981">+${roi['savings_yr']:,}</div></div>
            <div class="phev-metric"><div class="phev-metric-label">Payback Period</div><div class="phev-metric-val">{pay_str}</div></div>
            <div class="phev-metric"><div class="phev-metric-label">5yr Net</div><div class="phev-metric-val" style="color:{g5c}">{g5_str}</div></div>
            <div class="phev-metric"><div class="phev-metric-label">10yr Net</div><div class="phev-metric-val" style="color:{g10c}">{g10_str}</div></div>
          </div>
        </div>"""
        return header + f'<div class="phev-list">{cards}</div>'

    re_id_counter = [0]

    def render_re_table(items):
        if not items:
            return '<div class="no-deals">No listings found today.</div>'
        rows = ""
        for d in items:
            i = re_id_counter[0]
            re_id_counter[0] += 1
            dist_str = f"{d['distance']} mi" if d["distance"] is not None else "—"
            acres_str = f"{d['acres']:.2f}" if d["acres"] else "—"
            per_acre_str = f"${d['price'] // d['acres']:,.0f}" if d["acres"] else "—"
            cid = f"rc-{id(d)}"
            photos_html = ""
            for j, p in enumerate(d.get("photos", [])):
                photos_html += f'<img src="{p}" class="rc-img" onclick="showLightbox(\'{cid}\',{j})" />'
            carousel = f"""
            <div class="re-carousel" id="{cid}">
              <button class="rc-btn" onclick="rcScroll('{cid}', -1)">&#8249;</button>
              <div class="rc-strip" id="{cid}-strip">{photos_html}</div>
              <button class="rc-btn" onclick="rcScroll('{cid}', 1)">&#8250;</button>
            </div>""" if photos_html else ""
            pa_val = d['price'] // d['acres'] if d['acres'] else ""
            dist_val = d['distance'] if d['distance'] is not None else ""
            has_gis = d.get("lat") is not None and d.get("lon") is not None
            gis_btn = f'<button class="gis-btn" onclick="toggleGIS({i},{d["lat"]},{d["lon"]},{d.get("lot_sqft",43560)})">&#x1F5FA; GIS</button>' if has_gis else ""
            pid = prop_id(d['link'])
            dismiss_btn = f'<button class="dismiss-btn" onclick="dismissProp(\'{pid}\',this)">&#x2715; Dismiss</button>'
            p500 = monthly_payment(d['price'], 500000, mortgage_rate)
            p750 = monthly_payment(d['price'], 750000, mortgage_rate)
            mort_str = f"${p500:,}/mo" if p500 > 0 else "—"
            rows += f"""
          <tr data-idx="{i}" data-propid="{pid}" data-price="{d['price']}" data-acres="{d['acres'] or ''}" data-per-acre="{pa_val}" data-distance="{dist_val}">
            <td><a class="re-link" href="{d['link']}" target="_blank">{d['title']}</a><div class="re-source">{d['source']}</div><div class="re-actions">{gis_btn}{dismiss_btn}</div></td>
            <td class="re-num">${d['price']:,}</td>
            <td class="re-num">{acres_str}</td>
            <td class="re-num">{per_acre_str}</td>
            <td class="re-num">{dist_str}</td>
            <td class="re-num re-mort" data-p500="{p500}" data-p750="{p750}">{mort_str}</td>
          </tr>"""
            if carousel:
                rows += f"""
          <tr class="re-photo-row" data-for="{i}">
            <td colspan="6" class="re-photo-cell">{carousel}</td>
          </tr>"""
            if has_gis:
                rows += f"""
          <tr class="re-map-row" id="gisrow-{i}" data-for="{i}" style="display:none">
            <td colspan="6" class="re-map-cell">
              <div class="gis-stat" id="gisstat-{i}">Loading wetland data…</div>
              <div id="gismap-{i}" class="re-map"></div>
            </td>
          </tr>"""
        return f"""
        <table class="re-table">
          <thead><tr>
            <th>Listing</th>
            <th style="width:110px" class="re-sortable" onclick="sortRE(this,'price')">Price</th>
            <th style="width:72px"  class="re-sortable" onclick="sortRE(this,'acres')">Acres</th>
            <th style="width:100px" class="re-sortable" onclick="sortRE(this,'perAcre')">$/Acre</th>
            <th style="width:96px"  class="re-sortable" onclick="sortRE(this,'distance')">Distance ↗</th>
            <th style="width:110px">Est. Monthly</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>"""

    re_deals_html = ""
    for cat, items in re_deals.items():
        re_deals_html += f'<div class="subsection-title">{cat}</div>{render_re_table(items)}'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Daily Briefing — {date_str}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/@turf/turf@6/turf.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

  :root {{
    --bg: #07090f;
    --surface: #0e1420;
    --border: rgba(255,255,255,0.06);
    --accent: #4f8ef7;
    --green: #10b981;
    --red: #ef4444;
    --text: #e2e8f0;
    --muted: #475569;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    font-size: 18px;
    min-height: 100vh;
  }}

  body::before {{
    content: '';
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background:
      radial-gradient(ellipse at 15% 10%, rgba(79,142,247,0.07) 0%, transparent 45%),
      radial-gradient(ellipse at 85% 85%, rgba(16,185,129,0.05) 0%, transparent 45%);
    pointer-events: none;
    z-index: 0;
  }}

  .container {{
    max-width: 1080px;
    margin: 0 auto;
    padding: 56px 28px 80px;
    position: relative;
    z-index: 1;
  }}

  header {{ margin-bottom: 64px; }}

  .eyebrow {{
    font-size: 0.65rem;
    letter-spacing: 4px;
    text-transform: uppercase;
    color: var(--accent);
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    gap: 10px;
  }}

  .eyebrow::before {{
    content: '';
    display: inline-block;
    width: 24px;
    height: 1px;
    background: var(--accent);
  }}

  h1 {{
    font-size: clamp(2.2rem, 5vw, 3.5rem);
    font-weight: 700;
    letter-spacing: -1.5px;
    background: linear-gradient(135deg, #fff 0%, rgba(255,255,255,0.45) 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    line-height: 1.05;
    margin-bottom: 12px;
  }}

  .date {{ color: var(--muted); font-size: 1rem; font-weight: 400; }}

  section {{ margin-bottom: 60px; }}

  .section-title {{
    font-size: 0.6rem;
    letter-spacing: 3.5px;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    gap: 14px;
  }}

  .section-title::after {{
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border);
  }}

  .stock-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 10px;
  }}

  .stock-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px 18px;
    position: relative;
    overflow: hidden;
    transition: transform 0.2s ease, border-color 0.2s ease;
    cursor: default;
  }}

  .stock-card::before {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: var(--card-color);
    opacity: 0.8;
  }}

  .stock-card::after {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 60px;
    background: linear-gradient(180deg, var(--card-color) 0%, transparent 100%);
    opacity: 0.04;
    pointer-events: none;
  }}

  .stock-card:hover {{
    transform: translateY(-3px);
    border-color: rgba(255,255,255,0.1);
  }}

  .stock-ticker {{
    font-size: 0.6rem;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 10px;
  }}

  .stock-spark {{
    margin: 0 -18px 12px;
    line-height: 0;
    opacity: 0.85;
  }}

  .stock-footer {{
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 6px;
  }}

  .stock-price {{
    font-size: 1.1rem;
    font-weight: 600;
    color: #fff;
    font-variant-numeric: tabular-nums;
  }}

  .stock-change {{
    font-size: 0.78rem;
    font-weight: 500;
  }}

  .up {{ color: var(--green); }}
  .down {{ color: var(--red); }}
  .flat {{ color: var(--muted); }}

  .context-box {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-left: 2px solid var(--accent);
    border-radius: 0 14px 14px 0;
    padding: 22px 26px;
    color: #94a3b8;
    line-height: 1.85;
    font-size: 0.9rem;
  }}

  .news-list {{ display: flex; flex-direction: column; gap: 1px; }}

  .news-item {{
    display: flex;
    align-items: baseline;
    gap: 16px;
    padding: 13px 14px;
    border-radius: 10px;
    text-decoration: none;
    color: inherit;
    transition: background 0.15s;
  }}

  .news-item:hover {{ background: var(--surface); }}

  .news-source {{
    font-size: 0.58rem;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--accent);
    min-width: 58px;
    flex-shrink: 0;
  }}

  .news-title {{
    font-size: 0.88rem;
    color: #94a3b8;
    line-height: 1.45;
  }}

  .news-item:hover .news-title {{ color: #e2e8f0; }}

  .history-list {{ display: flex; flex-direction: column; gap: 1px; }}

  .history-item {{
    display: flex;
    align-items: baseline;
    gap: 22px;
    padding: 13px 14px;
    border-radius: 10px;
    transition: background 0.15s;
  }}

  .history-item:hover {{ background: var(--surface); }}

  .history-year {{
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--accent);
    min-width: 38px;
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }}

  .history-text {{
    font-size: 0.87rem;
    color: #64748b;
    line-height: 1.5;
  }}

  .ev-box {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    overflow: hidden;
  }}

  .ev-verdict {{
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 20px 24px;
    border-bottom: 1px solid var(--border);
  }}

  .ev-badge {{
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 2px;
    padding: 6px 14px;
    border-radius: 20px;
    border: 1px solid currentColor;
    flex-shrink: 0;
  }}

  .ev-verdict-note {{
    font-size: 0.88rem;
    color: #94a3b8;
    line-height: 1.5;
  }}

  .ev-body {{
    padding: 20px 24px;
    font-size: 0.88rem;
    color: #64748b;
    line-height: 1.75;
  }}

  .ev-movers {{
    margin-top: 10px;
    font-size: 0.78rem;
    color: var(--muted);
    letter-spacing: 0.5px;
  }}

  .deal-list {{ display: flex; flex-direction: column; gap: 8px; }}

  .deal-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 20px;
    display: flex;
    align-items: center;
    gap: 20px;
    text-decoration: none;
    color: inherit;
    transition: border-color 0.15s, transform 0.15s;
  }}

  .deal-card:hover {{
    border-color: rgba(255,255,255,0.12);
    transform: translateX(3px);
  }}

  .deal-discount {{
    font-size: 1.1rem;
    font-weight: 700;
    color: var(--green);
    min-width: 52px;
    text-align: center;
    flex-shrink: 0;
  }}

  .deal-info {{ flex: 1; min-width: 0; }}

  .deal-title {{
    font-size: 0.9rem;
    color: #e2e8f0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-bottom: 4px;
  }}

  .deal-meta {{
    font-size: 0.75rem;
    color: var(--muted);
  }}

  .deal-price {{
    font-size: 1rem;
    font-weight: 600;
    color: #fff;
    flex-shrink: 0;
    text-align: right;
  }}

  .deal-avg {{
    font-size: 0.72rem;
    color: var(--muted);
    text-decoration: line-through;
  }}

  .no-deals {{
    color: var(--muted);
    font-size: 0.88rem;
    padding: 16px 4px;
  }}

  .subsection-title {{
    font-size: 0.72rem;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--accent);
    margin: 24px 0 10px;
  }}

  .subsection-title:first-child {{ margin-top: 0; }}

  .re-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.86rem;
    table-layout: fixed;
  }}

  .re-table th {{
    text-align: left;
    font-size: 0.62rem;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--muted);
    padding: 0 14px 10px;
    border-bottom: 1px solid var(--border);
  }}

  .re-table th:not(:first-child) {{ text-align: right; }}

  .re-table td {{
    padding: 12px 14px;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
    overflow: hidden;
  }}

  .re-table tr:last-child td {{ border-bottom: none; }}

  .re-table tr:hover td {{ background: var(--surface); }}

  .re-link {{
    color: #e2e8f0;
    text-decoration: none;
    line-height: 1.4;
    display: block;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}

  .re-link:hover {{ color: var(--accent); }}

  .re-source {{
    font-size: 0.72rem;
    color: var(--muted);
    margin-top: 2px;
  }}

  .re-num {{
    text-align: right;
    font-variant-numeric: tabular-nums;
    color: #94a3b8;
    white-space: nowrap;
  }}

  .gis-btn {{
    display: inline-block;
    margin-top: 6px;
    background: none;
    border: 1px solid var(--border);
    color: var(--muted);
    font-family: inherit;
    font-size: 0.65rem;
    letter-spacing: 1px;
    padding: 3px 10px;
    border-radius: 12px;
    cursor: pointer;
    transition: all 0.15s;
  }}

  .gis-btn:hover {{
    border-color: var(--accent);
    color: var(--accent);
  }}

  .gis-btn.active {{
    background: rgba(79,142,247,0.12);
    border-color: var(--accent);
    color: var(--accent);
  }}

  .re-actions {{ display: flex; gap: 6px; margin-top: 6px; flex-wrap: wrap; }}

  .dismiss-btn {{
    background: none;
    border: 1px solid var(--border);
    color: var(--muted);
    font-family: inherit;
    font-size: 0.65rem;
    letter-spacing: 1px;
    padding: 3px 10px;
    border-radius: 12px;
    cursor: pointer;
    transition: all 0.15s;
  }}

  .dismiss-btn:hover {{
    border-color: #ef4444;
    color: #ef4444;
  }}

  .re-map-cell {{ padding: 0 14px 16px; }}

  .gis-stat {{
    font-size: 0.82rem;
    color: #94a3b8;
    padding: 10px 4px 8px;
  }}

  .gis-stat strong {{ font-size: 1rem; }}

  .re-map {{
    height: 340px;
    border-radius: 10px;
    overflow: hidden;
    border: 1px solid var(--border);
  }}

  .re-sortable {{
    cursor: pointer;
    user-select: none;
  }}

  .re-sortable:hover {{ color: var(--text); }}
  .re-sortable.sort-asc::after  {{ content: ' ▲'; color: var(--accent); }}
  .re-sortable.sort-desc::after {{ content: ' ▼'; color: var(--accent); }}

  .re-photo-row td {{ padding: 0; border-bottom: 1px solid var(--border); }}
  .re-photo-cell {{ padding: 10px 14px 14px; }}

  .re-carousel {{
    display: flex;
    align-items: center;
    gap: 8px;
  }}

  .rc-strip {{
    display: flex;
    gap: 6px;
    overflow-x: auto;
    flex: 1;
    scroll-behavior: smooth;
    scrollbar-width: none;
  }}

  .rc-strip::-webkit-scrollbar {{ display: none; }}

  .rc-img {{
    height: 130px;
    width: auto;
    border-radius: 7px;
    object-fit: cover;
    flex-shrink: 0;
    cursor: pointer;
    transition: opacity 0.15s, transform 0.15s;
  }}

  .rc-img:hover {{ opacity: 0.85; transform: scale(1.02); }}

  .rc-btn {{
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    font-size: 1.4rem;
    line-height: 1;
    padding: 6px 11px;
    border-radius: 8px;
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.15s;
  }}

  .rc-btn:hover {{ background: rgba(255,255,255,0.08); }}

  #lightbox {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.92);
    z-index: 9999;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    gap: 20px;
    padding: 24px;
  }}

  #lightbox.open {{ display: flex; }}

  .lb-center {{
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
    cursor: default;
  }}

  #lightbox-img {{
    max-width: 80vw;
    max-height: 82vh;
    border-radius: 10px;
    box-shadow: 0 24px 80px rgba(0,0,0,0.6);
  }}

  .lb-counter {{
    color: rgba(255,255,255,0.45);
    font-size: 0.78rem;
    letter-spacing: 1.5px;
  }}

  .lb-btn {{
    background: rgba(255,255,255,0.1);
    border: none;
    color: #fff;
    font-size: 2.4rem;
    line-height: 1;
    padding: 14px 18px;
    border-radius: 10px;
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.15s;
  }}

  .lb-btn:hover {{ background: rgba(255,255,255,0.2); }}

  .period-controls {{
    display: flex;
    gap: 6px;
    margin-bottom: 16px;
  }}

  .period-btn {{
    background: none;
    border: 1px solid var(--border);
    color: var(--muted);
    font-family: inherit;
    font-size: 0.68rem;
    letter-spacing: 1px;
    padding: 5px 13px;
    border-radius: 20px;
    cursor: pointer;
    transition: all 0.15s;
  }}

  .period-btn:hover {{
    border-color: rgba(255,255,255,0.15);
    color: var(--text);
  }}

  .period-btn.active {{
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }}

  .dp-controls {{
    display: flex;
    gap: 6px;
    margin-bottom: 14px;
  }}

  .dp-btn {{
    background: none;
    border: 1px solid var(--border);
    color: var(--muted);
    font-family: inherit;
    font-size: 0.68rem;
    letter-spacing: 1px;
    padding: 5px 13px;
    border-radius: 20px;
    cursor: pointer;
    transition: all 0.15s;
  }}

  .dp-btn:hover {{
    border-color: rgba(255,255,255,0.15);
    color: var(--text);
  }}

  .dp-btn.active {{
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }}

  /* ── PHEV Cards ── */
  .phev-assumptions {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-left: 2px solid var(--accent);
    border-radius: 0 10px 10px 0;
    padding: 14px 20px;
    margin-bottom: 20px;
    font-size: 0.8rem;
    color: #64748b;
    display: flex;
    gap: 20px;
    flex-wrap: wrap;
  }}
  .phev-assumptions strong {{ color: #94a3b8; }}
  .phev-list {{ display: flex; flex-direction: column; gap: 14px; }}
  .phev-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 18px 22px;
    transition: border-color 0.15s;
  }}
  .phev-card:hover {{ border-color: rgba(255,255,255,0.12); }}
  .phev-salvage {{ border-color: rgba(245,158,11,0.25) !important; }}
  .phev-card-top {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
    margin-bottom: 14px;
  }}
  .phev-card-title {{
    color: #e2e8f0;
    text-decoration: none;
    font-size: 0.95rem;
    font-weight: 500;
    display: block;
    margin-bottom: 3px;
  }}
  .phev-card-title:hover {{ color: var(--accent); }}
  .phev-card-meta {{ font-size: 0.74rem; color: var(--muted); display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
  .phev-model-tag {{
    background: rgba(79,142,247,0.12);
    color: var(--accent);
    font-size: 0.62rem;
    font-weight: 600;
    letter-spacing: 1px;
    padding: 2px 8px;
    border-radius: 10px;
    border: 1px solid rgba(79,142,247,0.25);
  }}
  .salvage-tag {{
    background: rgba(245,158,11,0.12);
    color: #f59e0b;
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 2px 8px;
    border-radius: 10px;
    border: 1px solid rgba(245,158,11,0.3);
  }}
  .phev-price-block {{ text-align: right; flex-shrink: 0; }}
  .phev-price-num {{ font-size: 1.4rem; font-weight: 700; color: #fff; }}
  .phev-price-vs {{ font-size: 0.78rem; margin-top: 2px; }}
  .phev-metrics {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
    gap: 8px;
  }}
  .phev-metric {{
    background: rgba(255,255,255,0.03);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 13px;
  }}
  .phev-metric-label {{ font-size: 0.62rem; color: var(--muted); letter-spacing: 0.5px; margin-bottom: 5px; text-transform: uppercase; }}
  .phev-metric-val {{ font-size: 0.92rem; font-weight: 600; color: #e2e8f0; }}
</style>
</head>
<body>
<div class="container">

  <header>
    <div class="eyebrow">Daily Intelligence</div>
    <h1>Morning Briefing</h1>
    <div class="date">{date_str}</div>
  </header>

  <section>
    <div class="section-title">Markets</div>
    <div class="period-controls">
      <button class="period-btn" data-period="1W">1W</button>
      <button class="period-btn" data-period="1M">1M</button>
      <button class="period-btn" data-period="3M">3M</button>
      <button class="period-btn" data-period="6M">6M</button>
      <button class="period-btn" data-period="1Y">1Y</button>
    </div>
    <div class="stock-grid">{stock_cards}</div>
  </section>

  <section>
    <div class="section-title">Market Context</div>
    <div class="context-box">{context}</div>
  </section>

  <section>
    <div class="section-title">EV Market</div>
    {ev_html}
  </section>

  <section>
    <div class="section-title">CTL Deals — PNW Craigslist</div>
    {deals_html}
  </section>

  <section>
    <div class="section-title">PHEV Market — Used Plug-in Hybrids (PNW)</div>
    {render_phev_cards(rav4_deals)}
  </section>

  <section>
    <div class="section-title">Real Estate Deals — PNW Craigslist</div>
    <div class="dp-controls">
      <button class="dp-btn active" data-dp="500" onclick="setDP(500)">$500k down</button>
      <button class="dp-btn" data-dp="750" onclick="setDP(750)">$750k down</button>
    </div>
    <div style="font-size:0.75rem;color:var(--muted);margin-bottom:14px;">30-yr fixed @ {mortgage_rate:.2f}% (FRED MORTGAGE30US)</div>
    {re_deals_html}
  </section>

  <section>
    <div class="section-title">World News</div>
    <div class="news-list">{news_items}</div>
  </section>

  <section>
    <div class="section-title">On This Day</div>
    <div class="history-list">{history_items}</div>
  </section>

</div>

<div id="lightbox" onclick="closeLightbox()">
  <button class="lb-btn" onclick="event.stopPropagation();lbStep(-1)">&#8249;</button>
  <div class="lb-center" onclick="event.stopPropagation()">
    <img id="lightbox-img" src="" />
    <div id="lightbox-counter" class="lb-counter"></div>
  </div>
  <button class="lb-btn" onclick="event.stopPropagation();lbStep(1)">&#8250;</button>
</div>

<script>
var _lb = {{ imgs: [], idx: 0 }};

function showLightbox(cid, idx) {{
  var strip = document.getElementById(cid + '-strip');
  _lb.imgs = Array.from(strip.querySelectorAll('img')).map(function(i) {{ return i.src; }});
  _lb.idx = idx;
  _lbRender();
  document.getElementById('lightbox').classList.add('open');
}}
function _lbRender() {{
  document.getElementById('lightbox-img').src = _lb.imgs[_lb.idx];
  document.getElementById('lightbox-counter').textContent = (_lb.idx + 1) + ' / ' + _lb.imgs.length;
}}
function lbStep(dir) {{
  _lb.idx = (_lb.idx + dir + _lb.imgs.length) % _lb.imgs.length;
  _lbRender();
}}
function closeLightbox() {{
  document.getElementById('lightbox').classList.remove('open');
}}
document.addEventListener('keydown', function(e) {{
  if (!document.getElementById('lightbox').classList.contains('open')) return;
  if (e.key === 'Escape')      closeLightbox();
  if (e.key === 'ArrowLeft')   lbStep(-1);
  if (e.key === 'ArrowRight')  lbStep(1);
}});

function dismissProp(pid, btn) {{
  fetch('/dismiss', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{id: pid}})
  }}).then(function(r) {{ return r.json(); }}).then(function() {{
    var tr = btn.closest('tr');
    var idx = tr.dataset.idx;
    tr.style.transition = 'opacity 0.35s';
    tr.style.opacity = '0';
    setTimeout(function() {{
      tr.style.display = 'none';
      document.querySelectorAll('tr[data-for="' + idx + '"]').forEach(function(r) {{
        r.style.display = 'none';
      }});
    }}, 350);
  }}).catch(function() {{
    alert('Could not save dismissal. Is the briefing server still running?');
  }});
}}

var _gisMaps = {{}};

function toggleGIS(idx, lat, lon, lotSqft) {{
  var row = document.getElementById('gisrow-' + idx);
  var btn = event.currentTarget;
  if (row.style.display === 'none') {{
    row.style.display = '';
    btn.classList.add('active');
    if (!_gisMaps[idx]) {{ _gisMaps[idx] = true; initGIS(idx, lat, lon, lotSqft); }}
  }} else {{
    row.style.display = 'none';
    btn.classList.remove('active');
  }}
}}

function initGIS(idx, lat, lon, lotSqft) {{
  var map = L.map('gismap-' + idx).setView([lat, lon], 16);

  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
    attribution: 'Tiles &copy; Esri'
  }}).addTo(map);

  L.tileLayer.wms('https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/services/Wetlands/MapServer/WMSServer', {{
    layers: '1',
    format: 'image/png',
    transparent: true,
    opacity: 0.55,
    attribution: 'USFWS NWI'
  }}).addTo(map);

  var areaM2 = lotSqft * 0.0929;
  var radiusKm = Math.sqrt(areaM2 / Math.PI) / 1000;
  var propPoly = turf.circle([lon, lat], radiusKm, {{ steps: 64, units: 'kilometers' }});

  L.geoJSON(propPoly, {{
    style: {{ color: '#4f8ef7', weight: 2, fillColor: '#4f8ef7', fillOpacity: 0.08 }}
  }}).addTo(map);

  L.circleMarker([lat, lon], {{
    radius: 6, color: '#4f8ef7', fillColor: '#4f8ef7', fillOpacity: 0.9, weight: 2
  }}).addTo(map);

  // Convert to Web Mercator for King County GIS query
  function toMerc(ln, lt) {{
    return [ln * 20037508.34 / 180,
            Math.log(Math.tan((90 + lt) * Math.PI / 360)) * 6378137];
  }}
  var mc = toMerc(lon, lat);
  var bufM = Math.max(Math.sqrt(areaM2) * 3, 1500);
  var geom = encodeURIComponent(JSON.stringify({{
    xmin: mc[0]-bufM, ymin: mc[1]-bufM, xmax: mc[0]+bufM, ymax: mc[1]+bufM,
    spatialReference: {{ wkid: 102100 }}
  }}));
  // King County SensitiveAreas layer 22 = NWI 2024 wetlands (queryable, unlike USFWS REST)
  var nwiUrl = 'https://gismaps.kingcounty.gov/arcgis/rest/services/Environment/KingCo_SensitiveAreas/MapServer/22/query'
    + '?geometry=' + geom
    + '&geometryType=esriGeometryEnvelope&spatialRel=esriSpatialRelIntersects'
    + '&outSR=4326&outFields=WETLAND_TYPE,NWI,ACRES&returnGeometry=true&f=geojson';

  fetch(nwiUrl)
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      var features = (data.features || []).filter(function(f) {{ return f.geometry; }});
      if (!features.length) {{
        document.getElementById('gisstat-' + idx).innerHTML = '<span style="color:#10b981">&#10003;</span> No wetlands detected (King County NWI 2024). Properties outside King County may show no data.';
        return;
      }}

      L.geoJSON(data, {{
        style: function(f) {{
          var t = (f.properties.WETLAND_TYPE || '').toLowerCase();
          var c = t.indexOf('emergent') >= 0 ? '#10b981' :
                  t.indexOf('forested') >= 0 ? '#059669' :
                  t.indexOf('scrub')    >= 0 ? '#34d399' : '#6ee7b7';
          return {{ color: c, fillColor: c, fillOpacity: 0.55, weight: 1 }};
        }}
      }}).addTo(map);

      var propArea = turf.area(propPoly);
      var wetArea = 0;
      features.forEach(function(f) {{
        try {{
          var inter = turf.intersect(propPoly, f);
          if (inter) wetArea += turf.area(inter);
        }} catch(e) {{}}
      }});
      var pct = Math.min(100, (wetArea / propArea * 100)).toFixed(1);
      var types = [...new Set(features.map(function(f) {{ return f.properties.WETLAND_TYPE || f.properties.NWI; }}).filter(Boolean))].join(', ');
      document.getElementById('gisstat-' + idx).innerHTML =
        '<strong style="color:' + (pct > 20 ? '#ef4444' : pct > 5 ? '#f59e0b' : '#10b981') + '">' + pct + '%</strong>'
        + ' of estimated property area is wetland'
        + (types ? ' &mdash; <span style="color:var(--muted);font-size:0.78rem">' + types + '</span>' : '');
    }})
    .catch(function() {{
      document.getElementById('gisstat-' + idx).textContent = 'Wetland data unavailable.';
    }});
}}

var _reSort = {{ col: null, dir: 1 }};
function sortRE(th, col) {{
  _reSort.dir = (_reSort.col === col) ? _reSort.dir * -1 : 1;
  _reSort.col = col;
  th.closest('thead').querySelectorAll('.re-sortable').forEach(function(h) {{
    h.classList.remove('sort-asc', 'sort-desc');
  }});
  th.classList.add(_reSort.dir === 1 ? 'sort-asc' : 'sort-desc');
  var tbody = th.closest('table').querySelector('tbody');
  var dataRows = Array.from(tbody.querySelectorAll('tr[data-idx]'));
  dataRows.sort(function(a, b) {{
    var av = parseFloat(a.dataset[col]);
    var bv = parseFloat(b.dataset[col]);
    if (isNaN(av)) av = _reSort.dir === 1 ? Infinity : -Infinity;
    if (isNaN(bv)) bv = _reSort.dir === 1 ? Infinity : -Infinity;
    return (av - bv) * _reSort.dir;
  }});
  dataRows.forEach(function(row) {{
    var photo = tbody.querySelector('tr[data-for="' + row.dataset.idx + '"]');
    tbody.appendChild(row);
    if (photo) tbody.appendChild(photo);
  }});
}}

function rcScroll(id, dir) {{
  var strip = document.getElementById(id + '-strip');
  strip.scrollBy({{ left: dir * 400, behavior: 'smooth' }});
}}

(function() {{
  var SLICES = {{ '1W': 5, '1M': 21, '3M': 63, '6M': 126, '1Y': Infinity }};

  function makeSpark(prices, color) {{
    var n = prices.length;
    if (n < 2) return '';
    var lo = Math.min.apply(null, prices), hi = Math.max.apply(null, prices);
    var spread = hi - lo || 1, padY = 3, vw = 100, vh = 48;
    var pts = prices.map(function(p, i) {{
      var x = (i / (n - 1)) * vw;
      var y = padY + (1 - (p - lo) / spread) * (vh - 2 * padY);
      return x.toFixed(2) + ',' + y.toFixed(2);
    }}).join(' ');
    return '<svg width="100%" height="48" viewBox="0 0 100 48" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
      + '<polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
      + '</svg>';
  }}

  function setPeriod(period) {{
    var n = SLICES[period];
    document.querySelectorAll('.stock-card').forEach(function(card) {{
      var all = JSON.parse(card.dataset.spark);
      var prices = (n === Infinity) ? all : all.slice(-n);
      var curr = prices[prices.length - 1], first = prices[0];
      var pct = ((curr - first) / first) * 100;
      var up = pct > 0, down = pct < 0;
      var color = up ? '#10b981' : down ? '#ef4444' : '#475569';
      var arrow = up ? '▲' : down ? '▼' : '—';
      var cls = up ? 'up' : down ? 'down' : 'flat';
      card.style.setProperty('--card-color', color);
      card.querySelector('.stock-spark').innerHTML = makeSpark(prices, color);
      var chgEl = card.querySelector('.stock-change');
      chgEl.className = 'stock-change ' + cls;
      chgEl.textContent = arrow + ' ' + Math.abs(pct).toFixed(2) + '%';
    }});
    document.querySelectorAll('.period-btn').forEach(function(btn) {{
      btn.classList.toggle('active', btn.dataset.period === period);
    }});
  }}

  document.querySelectorAll('.period-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{ setPeriod(btn.dataset.period); }});
  }});

  setPeriod('1M');
}})();

function setDP(k) {{
  document.querySelectorAll('.dp-btn').forEach(function(b) {{
    b.classList.toggle('active', b.dataset.dp == k);
  }});
  document.querySelectorAll('.re-mort').forEach(function(td) {{
    var v = parseInt(td.dataset['p' + k]);
    td.textContent = v > 0 ? '$' + v.toLocaleString() + '/mo' : '—';
  }});
}}
</script>
</body>
</html>"""

# --- MAIN ---
print("Fetching markets...")
stocks = get_stocks()
print("Fetching EV market...")
ev_stocks = get_ev_stocks()
print("Fetching news...")
news = get_news()
print("Fetching history...")
history = get_history()
print("Scanning Craigslist for RAV4 Prime deals...")
rav4_deals = get_rav4_deals()
print(f"RAV4 results: {len(rav4_deals)}")
print("Scanning PNW Craigslist for CTL deals...")
deals = get_equipment_deals()
print("CTL results:", {k: len(v) for k, v in deals.items()})
print("Fetching real estate listings (Realtor.com MLS + Craigslist FSBO)...")
re_deals = get_realestate_deals()
print("Real estate results:", {k: len(v) for k, v in re_deals.items()})
print("Fetching mortgage rate...")
mortgage_rate = get_mortgage_rate()
print(f"Mortgage rate: {mortgage_rate}%")

# Filter dismissed properties before building HTML
dismissed = load_dismissed()
for cat in re_deals:
    re_deals[cat] = [d for d in re_deals[cat] if prop_id(d['link']) not in dismissed]
print(f"Dismissed filter: {len(dismissed)} properties hidden.")

path = os.path.join(_LIFE_DIR, "briefing.html")
with open(path, "w") as f:
    f.write(build_html(stocks, news, history, ev_stocks, deals, rav4_deals, re_deals, mortgage_rate))

# Local server — handles dismiss POSTs and serves the HTML
class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', '/briefing'):
            with open(path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/dismiss':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n))
            pid = body.get('id', '')
            if pid:
                d = load_dismissed()
                d.add(pid)
                save_dismissed(d)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass

if not os.environ.get("CI"):
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(('', BRIEFING_PORT), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"Serving briefing at http://localhost:{BRIEFING_PORT} — press Ctrl+C to stop")
    webbrowser.open(f"http://localhost:{BRIEFING_PORT}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        httpd.shutdown()
        print("Done.")
