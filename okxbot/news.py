"""Simple, robust news sentiment from free public RSS feeds (no API key).
Keyword-lexicon scoring of headlines + summaries from the last 24h.
Returns a market-wide score and per-coin scores in [-1, 1]. Failures degrade to 0 (neutral)."""
import datetime as dt, email.utils, html, json, os, re, time
import xml.etree.ElementTree as ET
import requests
from okxbot import config

FEEDS = [
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]
POS = {"surge": 2, "surges": 2, "soar": 2, "soars": 2, "rally": 2, "rallies": 2, "jump": 1, "jumps": 1,
       "gain": 1, "gains": 1, "bull": 1, "bullish": 2, "record high": 2, "all-time high": 2, "ath": 1,
       "approve": 2, "approved": 2, "approval": 2, "inflow": 1, "inflows": 1, "adoption": 1, "partnership": 1,
       "upgrade": 1, "breakout": 1, "rebound": 1, "recover": 1, "recovers": 1, "buy": 1, "accumulate": 1,
       "launch": 1, "etf": 0.5, "rate cut": 1, "outperform": 1}
NEG = {"crash": 2, "crashes": 2, "plunge": 2, "plunges": 2, "dump": 2, "dumps": 2, "drop": 1, "drops": 1,
       "fall": 1, "falls": 1, "slide": 1, "slump": 2, "bear": 1, "bearish": 2, "hack": 3, "hacked": 3,
       "exploit": 3, "exploited": 3, "lawsuit": 2, "sue": 2, "sues": 2, "sec charges": 3, "ban": 2, "bans": 2,
       "outflow": 1, "outflows": 1, "liquidation": 1, "liquidations": 1, "fraud": 3, "scam": 2, "delist": 3,
       "delisting": 3, "investigation": 2, "selloff": 2, "sell-off": 2, "fear": 1, "warning": 1, "rate hike": 1,
       "bankrupt": 3, "insolvency": 3, "depeg": 3, "outage": 2, "halt": 1, "underperform": 1}
NAMES = {"BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "ether", "eth"], "SOL": ["solana", "sol"],
         "XRP": ["xrp", "ripple"], "DOGE": ["dogecoin", "doge"], "ADA": ["cardano", "ada"],
         "BNB": ["bnb", "binance coin"], "SUI": ["sui"], "AVAX": ["avalanche", "avax"], "LINK": ["chainlink"],
         "DOT": ["polkadot"], "LTC": ["litecoin", "ltc"], "TON": ["toncoin", "ton"], "TRX": ["tron", "trx"],
         "PEPE": ["pepe"], "NEAR": ["near protocol"], "HYPE": ["hyperliquid"], "UNI": ["uniswap"],
         "WLD": ["worldcoin"], "FIL": ["filecoin"], "ZEC": ["zcash", "zec"], "ONDO": ["ondo"],
         "TRUMP": ["trump coin", "$trump", "official trump"], "STRK": ["starknet"], "SAND": ["the sandbox"]}
CACHE = os.path.join(config.DATA_DIR, "news_cache.json")


def _score(text):
    t = text.lower()
    s = 0.0
    for w, v in POS.items():
        s += v * len(re.findall(r"\b" + re.escape(w) + r"\b", t))
    for w, v in NEG.items():
        s -= v * len(re.findall(r"\b" + re.escape(w) + r"\b", t))
    return s


def fetch_items(max_age_h=24):
    items, errors = [], []
    now = dt.datetime.now(dt.timezone.utc)
    for url in FEEDS:
        try:
            r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))[:400]
                pub = it.findtext("pubDate")
                try:
                    ts = email.utils.parsedate_to_datetime(pub)
                except Exception:
                    ts = now
                age_h = (now - ts).total_seconds() / 3600
                if age_h <= max_age_h:
                    items.append({"title": title, "text": f"{title}. {desc}", "age_h": round(age_h, 1),
                                  "src": url.split("/")[2],
                                  "url": (it.findtext("link") or "").strip(),
                                  "published_at": ts.astimezone(dt.timezone.utc).isoformat(),
                                  "summary": desc})
        except Exception as e:
            errors.append(f"{url}: {type(e).__name__}: {e}"[:200])
    return items, errors


def sentiment(symbols):
    """Returns {"market": float, "coins": {SYM: float}, "n": int, "errors": [...], "top": [...]}"""
    items, errors = fetch_items()
    def agg(sel):
        if not sel:
            return 0.0, 0
        num = sum(_score(i["text"]) * (0.5 ** (i["age_h"] / 12)) for i in sel)  # 12h half-life
        den = sum(0.5 ** (i["age_h"] / 12) for i in sel)
        return max(-1.0, min(1.0, num / den / 3)), len(sel)
    market, n = agg(items)
    coins = {}
    for sym in symbols:
        keys = NAMES.get(sym, [sym.lower()])
        sel = [i for i in items if any(re.search(r"\b" + re.escape(k) + r"\b", i["text"].lower()) for k in keys)]
        if sel:
            coins[sym] = round(agg(sel)[0], 3)
    out = {"market": round(market, 3), "coins": coins, "n": n, "errors": errors, "high_impact": high_impact(items),
           "top": sorted(({"title": i["title"], "score": _score(i["text"])} for i in items),
                         key=lambda x: -abs(x["score"]))[:5],
           "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        json.dump(out, open(CACHE, "w"), indent=1, ensure_ascii=False)
    except Exception:
        pass
    return out


# ---- High-impact negative news detection ----
# Always high-impact (systemic): depeg, insolvency, withdrawal halts, SEC enforcement, country bans.
HI_ALWAYS = [r"\bdepeg(ged|s)?\b", r"\blos(es|t) (its )?peg\b", r"\binsolven", r"\bbankrupt",
             r"\b(halts?|suspend(s|ed)?|freez(es|e)|froze) withdrawals?\b", r"\bsec (sues|charges|lawsuit)\b",
             r"\bsec sued\b", r"\b(bans?|banned|outlaws?) (crypto|bitcoin|cryptocurrenc)", r"\bcrypto ban\b",
             r"\bexchange (collapse|collapses|collapsed)\b", r"\bcircuit breaker\b"]
# Hack/exploit/delisting only counts if it hits a major venue/asset or is large ($100M+).
HI_CONDITIONAL = [r"\bhack(ed|s|er|ers)?\b", r"\bexploit(ed|s)?\b", r"\bdrain(ed|s)?\b", r"\bdelist(s|ed|ing)?\b",
                  r"\bstolen\b"]
MAJOR = [r"\b(binance|coinbase|okx|bybit|kraken|bitget|kucoin|gate\.io|htx|upbit|robinhood|tether|usdt|circle|usdc|"
         r"lido|metamask|ledger|bitcoin|btc|ethereum|eth|solana|xrp)\b"]
BIG_USD = re.compile(r"\$\s?(\d+(?:\.\d+)?)\s?(billion|bn|b\b|million|m\b)", re.I)


_ENTITY_STOP = {"the", "this", "that", "what", "why", "how", "after", "before", "with", "from", "amid", "over",
                "breaking", "report", "reports", "update", "news", "crypto", "market", "markets", "price", "prices",
                "says", "said", "here", "will", "could", "would", "should", "new", "and", "for", "its", "as"}


def _events(text):
    t = text.lower()
    return {p for p in HI_ALWAYS + HI_CONDITIONAL if re.search(p, t)}


def _entities(title):
    t = title.lower()
    out = {m.group(0) for p in MAJOR for m in re.finditer(p, t)}
    out |= {sym for sym, names in NAMES.items() if any(re.search(r"\b" + re.escape(n) + r"\b", t) for n in names)}
    # proper nouns (capitalised, not the first word), e.g. venue / project / regulator names
    words = re.findall(r"[A-Za-z][A-Za-z0-9.$]+", title)
    out |= {w.lower() for w in words[1:] if w[0].isupper() and len(w) >= 3 and w.lower() not in _ENTITY_STOP}
    return out


def same_story(title_a, title_b):
    """Cross-outlet confirmation (2026-10-19, tightened): both headlines must describe the SAME high-impact
    event type (same HI pattern, e.g. 'hacked' / 'halts withdrawals') about the SAME entity (venue, asset or
    proper noun). Sharing generic words such as 'bitcoin price' is no longer enough."""
    return bool(_events(title_a) & _events(title_b)) and bool(_entities(title_a) & _entities(title_b))


def is_high_impact(text):
    t = text.lower()
    for p in HI_ALWAYS:
        if re.search(p, t):
            return "systemic: " + p.strip("\\b")
    if any(re.search(p, t) for p in HI_CONDITIONAL):
        if any(re.search(p, t) for p in MAJOR):
            return "hack/exploit/delist on major venue/asset"
        for m in BIG_USD.finditer(text):
            v = float(m.group(1)) * (1000 if m.group(2).lower().startswith("b") else 1)
            if v >= 100:
                return f"hack/exploit >= $100M (${v:.0f}M)"
    return None


def high_impact(items, hours=None):
    hours = hours if hours is not None else getattr(config, "NEWS_HIGH_IMPACT_HOURS", 6)
    out = []
    for i in items:
        if i["age_h"] > hours:
            continue
        why = is_high_impact(i["text"])
        if why:
            out.append({"title": i["title"], "src": i["src"], "age_h": i["age_h"], "why": why})
    return out


if __name__ == "__main__":
    print(json.dumps(sentiment(["BTC", "ETH", "SOL", "XRP", "DOGE"]), indent=1, ensure_ascii=False))
