"""Funding-rate helpers (public OKX endpoints, no keys needed).
Rates are normalized to an 8h-equivalent so instruments with 1h/4h funding intervals compare fairly."""
import json, os, time
import requests
from okxbot import config

URL = config.BASE_URL + "/api/v5/public/funding-rate"
HIST_URL = config.BASE_URL + "/api/v5/public/funding-rate-history"
CACHE_DIR = os.path.join(config.DATA_DIR, "funding_cache")


def all_rates():
    """{instId: {"rate": current-period rate, "interval_h": h, "rate_8h": rate*8/h, "next_ts": ms}} for all swaps."""
    j = requests.get(URL, params={"instId": "ANY"}, timeout=15).json()
    if j.get("code") != "0":
        raise RuntimeError(f"funding-rate ANY: {j.get('code')} {j.get('msg')}")
    out = {}
    for d in j["data"]:
        try:
            rate = float(d["fundingRate"])
            ft, prev = int(d["fundingTime"]), int(d.get("prevFundingTime") or 0)
            nxt = int(d.get("nextFundingTime") or 0)
            ih = (ft - prev) / 3.6e6 if prev else ((nxt - ft) / 3.6e6 if nxt else 8.0)
            ih = ih if ih > 0 else 8.0
            out[d["instId"]] = {"rate": rate, "interval_h": round(ih, 2), "rate_8h": rate * 8.0 / ih,
                                "funding_ts": ft}
        except (KeyError, ValueError):
            continue
    return out


def pays_funding(side, rate):
    """Longs pay when rate > 0, shorts pay when rate < 0."""
    return (side == 1 and rate > 0) or (side == -1 and rate < 0)


def entry_blocked(side, info):
    """True if a new position on this side would PAY funding above the threshold (8h-equivalent)."""
    if not info:
        return False
    return pays_funding(side, info["rate_8h"]) and abs(info["rate_8h"]) > config.FUNDING_MAX_PAY_8H


def history(inst_id, max_pages=40):
    """Settled funding history (oldest first) as [(ts_ms, rate)]. OKX serves only ~3 months back."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    f = os.path.join(CACHE_DIR, f"{inst_id}.json")
    if os.path.exists(f) and time.time() - os.path.getmtime(f) < 86400:
        return [tuple(x) for x in json.load(open(f))]
    rows, after = [], None
    for _ in range(max_pages):
        p = {"instId": inst_id, "limit": 100}
        if after:
            p["after"] = after
        j = requests.get(HIST_URL, params=p, timeout=15).json()
        d = j.get("data") or []
        if not d:
            break
        rows += [(int(x["fundingTime"]), float(x.get("realizedRate") or x["fundingRate"])) for x in d]
        after = d[-1]["fundingTime"]
        time.sleep(0.12)
    rows = sorted(set(rows))
    json.dump(rows, open(f, "w"))
    return rows


def last_settled(inst_id):
    """Most recent settled funding rate (raw, per interval) or None."""
    try:
        d = requests.get(HIST_URL, params={"instId": inst_id, "limit": 1}, timeout=10).json().get("data") or []
        return float(d[0].get("realizedRate") or d[0]["fundingRate"]) if d else None
    except Exception:
        return None
