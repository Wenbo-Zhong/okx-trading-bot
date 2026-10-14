"""Watchlist construction: majors, weekly-rotated alts, US stock tokens, commodities, plus held positions."""
import datetime as dt, json, math, os, time
from zoneinfo import ZoneInfo
from okxbot import config

SH = ZoneInfo(config.TIMEZONE)
CACHE = os.path.join(config.DATA_DIR, "monitor_watchlist.json")
STABLE = set(config.EXCLUDE_BASE)


def swap(base):
    return f"{base}-USDT-SWAP"


def base(inst):
    return inst.split("-")[0]


def usdt_volume(t):
    """24h USDT notional of a USDT-margined swap ticker."""
    try:
        return float(t.get("volCcy24h") or 0) * float(t.get("last") or 0)
    except ValueError:
        return 0.0


def spread_pct(t):
    try:
        bid, ask = float(t["bidPx"]), float(t["askPx"])
    except (KeyError, ValueError, TypeError):
        return math.inf
    if bid <= 0 or ask <= 0:
        return math.inf
    return (ask - bid) / ((ask + bid) / 2) * 100


def daily_vol_pct(closes):
    """Stdev of daily simple returns (percent) over the given closes (oldest first)."""
    r = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(r) < 3:
        return None
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1)) * 100


def volume_pool(tickers, crypto_ids, top_n):
    rows = [t for t in tickers if t.get("instId", "").endswith("-USDT-SWAP") and t["instId"] in crypto_ids]
    rows.sort(key=usdt_volume, reverse=True)
    return rows[:top_n]


def select_alts(pool, cfg, exclude, flagged, vol_of, depth_of=None):
    """Apply the hard filters in order. Returns (selected instIds, {instId: reason rejected})."""
    picked, rejected = [], {}
    for t in pool:
        inst = t["instId"]
        b = base(inst)
        if b in exclude:
            rejected[inst] = "excluded"; continue
        if inst in flagged:
            rejected[inst] = "flagged/delisting"; continue
        sp = spread_pct(t)
        if sp >= cfg["alt_max_spread_pct"]:
            rejected[inst] = f"spread {sp:.3f}%"; continue
        v = vol_of(inst)
        if v is None or not (cfg["alt_vol_min_pct"] <= v <= cfg["alt_vol_max_pct"]):
            rejected[inst] = f"7d vol {v if v is None else round(v, 2)}%"; continue
        if depth_of is not None:
            d = depth_of(inst)
            if d is not None and d < cfg["alt_min_top_depth_usdt"]:
                rejected[inst] = f"thin book {d:.0f} USDT"; continue
        picked.append(inst)
        if len(picked) >= cfg["alt_count"]:
            break
    return picked, rejected


def pick_stocks(tickers, cfg, available):
    vol = {t["instId"]: usdt_volume(t) for t in tickers}
    core = [swap(b) for b in cfg["stock_core"] if swap(b) in available]
    cands = [swap(b) for b in cfg["stock_candidates"] if swap(b) in available and swap(b) not in core]
    cands.sort(key=lambda i: vol.get(i, 0), reverse=True)
    return core + cands[:cfg["stock_extra"]]


def rotation_key(now=None):
    """Week id that changes on the configured weekday (Monday) in Asia/Shanghai."""
    t = (now or dt.datetime.now(dt.timezone.utc)).astimezone(SH)
    y, w, _ = t.isocalendar()
    return f"{y}-W{w:02d}"


def held_instruments(state):
    out = set(state.get("positions", {}))
    for inst in state.get("spot", {}).get("longs", {}) or {}:
        out.add(inst)
    return sorted(out)


def build(client, state, cfg, now=None, force=False, cache_path=CACHE):
    """Return {"instruments": {instId: {"group", "bars"}}, "alts_rejected", "rotation"}."""
    key = rotation_key(now)
    cached = None
    try:
        with open(cache_path) as f:
            cached = json.load(f)
    except (FileNotFoundError, ValueError):
        pass
    insts = client.instruments("SWAP")
    live = {i["instId"]: i for i in insts if i.get("state") == "live"}
    crypto = {i for i, d in live.items() if d.get("instCategory") in ("1", None, "")}
    tickers = [t for t in client.tickers("SWAP") if t.get("instId") in live]
    held = held_instruments(state)
    if cached and cached.get("rotation") == key and not force:
        alts, rejected = cached["alts"], cached.get("alts_rejected", {})
    else:
        exclude = (set(cfg["majors"]) | set(cfg["alt_exclude_always"]) | set(cfg["user_exclude"]) | STABLE
                   | {base(h) for h in held})
        flagged = {i for i, d in live.items() if d.get("ruleType") not in (None, "", "normal")}
        pool = volume_pool(tickers, crypto, cfg["alt_top_n_volume"])

        def vol_of(inst):
            rows = client.candles(inst, bar="1Dutc", limit=8)
            time.sleep(cfg["rest_min_gap_s"])
            closes = [float(r[4]) for r in reversed(rows)]
            return daily_vol_pct(closes)

        def depth_of(inst):
            try:
                ob = client._req("GET", "/api/v5/market/books", {"instId": inst, "sz": 400})[0]
            except Exception:
                return None
            time.sleep(cfg["rest_min_gap_s"])
            ct = float(live[inst].get("ctVal") or 1)
            mid = (float(ob["bids"][0][0]) + float(ob["asks"][0][0])) / 2
            band = cfg["alt_depth_band_pct"] / 100
            side = lambda rows: sum(float(p) * float(s) * ct for p, s, *_ in rows if abs(float(p) / mid - 1) <= band)
            return min(side(ob["bids"]), side(ob["asks"]))

        alts, rejected = select_alts(pool, cfg, exclude, flagged, vol_of, depth_of)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump({"rotation": key, "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                       "alts": alts, "alts_rejected": rejected}, f, indent=1)
    out = {}

    def add(inst, group, bars):
        if inst not in live and not inst.endswith("-USDT"):
            return
        e = out.setdefault(inst, {"group": group, "bars": []})
        e["bars"] = sorted(set(e["bars"]) | set(bars))

    for b in cfg["majors"]:
        add(swap(b), "major", cfg["major_bars"])
    for i in alts:
        add(i, "alt", cfg["alt_bars"])
    for i in pick_stocks(tickers, cfg, live):
        add(i, "stock", cfg["stock_bars"])
    for b in cfg["commodities"]:
        add(swap(b), "commodity", cfg["commodity_bars"])
    for h in held:
        if h in out:
            out[h]["held"] = True
            out[h]["bars"] = sorted(set(out[h]["bars"]) | set(cfg["held_bars"]))
        else:
            add(h, "held", cfg["held_bars"]); out[h]["held"] = True
    return {"instruments": out, "alts_rejected": rejected, "rotation": key}
