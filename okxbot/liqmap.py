"""Estimated liquidation map (reference only, never an entry signal).

CoinGlass heatmap API needs a paid Professional plan; there is no free heatmap API, so we ESTIMATE
from free OKX public data (production):
  * 1h candles (/api/v5/market/candles) and hourly open interest per instrument
    (/api/v5/rubik/stat/contracts/open-interest-history, ~60 days available).
Model (approximation, same idea as public "liquidation heatmap" models):
  * each hour with rising OI: new notional = dOI(base ccy) x typical price, split 50/50 long/short,
    spread over assumed leverage tiers LEV_TIERS; liq price long = p*(1-1/L+MMR), short = p*(1+1/L-MMR)
  * each hour with falling OI: all open estimated levels shrink pro rata (positions closed)
  * a level is removed once price trades through it (assumed liquidated / stopped)
Output: remaining levels bucketed in BUCKET_PCT price bins. Units are rough USD notional; only the
RELATIVE density matters. Realized liquidations (/api/v5/public/liquidation-orders) are used for sanity only."""
import time
import numpy as np
import requests
from okxbot import config

LEV_TIERS = {5: 0.10, 10: 0.30, 25: 0.30, 50: 0.20, 100: 0.10}
MMR = 0.005
BUCKET_PCT = 0.0025          # 0.25% price buckets
DENSE_MULT = 2.5             # bucket is "dense" if >= 2.5x the average 0.25% bucket within +-5% (that side)
NEAR_BELOW_ATR = 0.5         # (long) cluster up to 0.5 ATR beyond the stop ...
NEAR_ABOVE_ATR = 1.0         # ... or up to 1 ATR between stop and price counts as "near"
BUFFER_ATR = 0.25            # new stop = far edge of cluster + 0.25 ATR
OI_URL = config.BASE_URL + "/api/v5/rubik/stat/contracts/open-interest-history"
_cache = {}


def oi_history(inst_id, pages=8):
    """{ts_ms: oi_base_ccy} hourly, oldest ~60 days."""
    if inst_id in _cache and time.time() - _cache[inst_id][0] < 600:
        return _cache[inst_id][1]
    out, end = {}, None
    for _ in range(pages):
        q = {"instId": inst_id, "period": "1H", "limit": 100}
        if end:
            q["end"] = end
        d = requests.get(OI_URL, params=q, timeout=15).json().get("data") or []
        if not d:
            break
        for r in d:
            out[int(r[0])] = float(r[2])
        end = d[-1][0]
        time.sleep(0.2)
    _cache[inst_id] = (time.time(), out)
    return out


def build_levels(bars, oi):
    """bars: list of (ts, o, h, l, c) oldest first (closed 1h bars). oi: {ts: oi_ccy} snapshots.
    Returns np.array of [price, usd, side] for surviving levels (side +1 = long liq, -1 = short liq)."""
    lv_px, lv_usd, lv_side = [], [], []
    px = np.empty(0); usd = np.empty(0); side = np.empty(0)
    for ts, o, h, l, c in bars:
        if len(px):
            # levels crossed by this bar are gone
            alive = ~(((side == 1) & (px >= l)) | ((side == -1) & (px <= h)))
            px, usd, side = px[alive], usd[alive], side[alive]
        a, b = oi.get(ts), oi.get(ts + 3600_000)
        if a is None or b is None or a <= 0:
            continue
        d = b - a
        if d < 0 and len(usd):
            usd = usd * max(0.0, 1 + d / a)
        elif d > 0:
            p = (h + l + c) / 3
            n = d * p
            new_px, new_usd, new_side = [], [], []
            for L, w in LEV_TIERS.items():
                new_px += [p * (1 - 1 / L + MMR), p * (1 + 1 / L - MMR)]
                new_usd += [n * w * 0.5] * 2
                new_side += [1, -1]
            px = np.r_[px, new_px]; usd = np.r_[usd, new_usd]; side = np.r_[side, new_side]
    return np.c_[px, usd, side] if len(px) else np.empty((0, 3))


def buckets(levels, ref_px, side=None, span=0.10):
    """{bucket_index: usd} with bucket k covering ref*(1+k*B) .. ref*(1+(k+1)*B), within +-span."""
    out = {}
    for p, u, s in levels:
        if side is not None and s != side:
            continue
        r = p / ref_px - 1
        if abs(r) > span:
            continue
        k = int(np.floor(r / BUCKET_PCT))
        out[k] = out.get(k, 0.0) + u
    return out


def dense_threshold(bk, span=0.05):
    """Dense = >= DENSE_MULT x the average bucket (empty buckets included) within +-span of price, one side."""
    nb = int(round(span / BUCKET_PCT))
    tot = sum(u for k, u in bk.items() if -nb <= k < nb)
    side_buckets = nb  # liquidation levels of one side lie on one side of price
    return DENSE_MULT * tot / side_buckets if tot > 0 else float("inf")


def adjust_stop(side, entry, stop, atr, levels, near_toward_atr=None, max_check=True):
    """Nudge the stop beyond a dense liquidation cluster sitting near it. Returns (new_stop, info|None).
    Long: long-liquidation clusters below price; short: short-liquidation clusters above price.
    Never moves the stop closer to price; gives up (keeps original) if it would exceed MAX_STOP_PCT."""
    if levels is None or not len(levels):
        return stop, None
    bk = buckets(levels, entry, side=side)
    th = dense_threshold(bk)
    lo_px = stop - side * NEAR_BELOW_ATR * atr     # beyond the stop
    hi_px = stop + side * (NEAR_ABOVE_ATR if near_toward_atr is None else near_toward_atr) * atr  # toward price
    # (entry = reference price: entry price for new trades, current price for open positions)
    hits = []
    for k, u in bk.items():
        if u < th:
            continue
        b_lo, b_hi = entry * (1 + k * BUCKET_PCT), entry * (1 + (k + 1) * BUCKET_PCT)
        if side == 1 and b_hi >= lo_px and b_lo <= min(hi_px, entry):
            hits.append((b_lo, b_hi, u))
        if side == -1 and b_lo <= lo_px and b_hi >= max(hi_px, entry):
            hits.append((b_lo, b_hi, u))
    if not hits:
        return stop, None
    if side == 1:
        far = min(h[0] for h in hits); new = min(stop, far - BUFFER_ATR * atr)
    else:
        far = max(h[1] for h in hits); new = max(stop, far + BUFFER_ATR * atr)
    info = {"clusters": [(round(a, 6), round(b, 6), round(u)) for a, b, u in sorted(hits)],
            "old_stop": stop, "new_stop": new, "dist_pct": round(abs(entry - new) / entry * 100, 3)}
    if max_check and abs(entry - new) / entry > config.MAX_STOP_PCT:
        info["kept_original"] = "nudged stop would exceed MAX_STOP_PCT"
        return stop, info
    return new, info


def current_levels(x, inst_id, n_bars=720):
    """Live: build the map from production candles + OI history. x = OKX client."""
    rows = x.recent_candles(inst_id, "1H", n_bars)
    bars = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows if r[8] == "1"]
    return build_levels(bars, oi_history(inst_id)), (bars[-1][4] if bars else None)


def nearest_clusters(levels, ref_px, span=0.03, top=2):
    """Largest dense buckets within +-span of price: [(side, lo, hi, usd, dist_pct)]."""
    out = []
    for s in (1, -1):
        bk = buckets(levels, ref_px, side=s)
        th = dense_threshold(bk)
        for k, u in bk.items():
            lo, hi = ref_px * (1 + k * BUCKET_PCT), ref_px * (1 + (k + 1) * BUCKET_PCT)
            mid = (lo + hi) / 2
            if u >= th and abs(mid / ref_px - 1) <= span:
                out.append((s, lo, hi, u, (mid / ref_px - 1) * 100))
    return sorted(out, key=lambda r: -r[3])[:top]


def realized(inst_family, limit=100):
    """Recent realized liquidations on OKX (sanity reference; only the most recent ~100 records)."""
    j = requests.get(config.BASE_URL + "/api/v5/public/liquidation-orders",
                     params={"instType": "SWAP", "uly": inst_family, "state": "filled", "limit": limit}, timeout=15).json()
    d = (j.get("data") or [{}])[0].get("details", [])
    return [(int(r["ts"]), float(r["bkPx"]), float(r["sz"]), r["posSide"]) for r in d]
