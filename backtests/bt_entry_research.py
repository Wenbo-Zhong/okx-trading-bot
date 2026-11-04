"""Research: why entries are rare, and candidate entry styles that raise frequency
without wrecking OOS expectancy/DD. DEMO research only — does not change planner.

Uses OKX public 1H history (up to ~2y). Fees: taker 0.05%/side + 0.03% slip for market;
pullback/limit legs use maker 0.02% + 0.01% slip. Guardrails match live config
(MAX_POSITIONS, RISK_PER_TRADE, 2% daily kill, 40% DD halt).
"""
from __future__ import annotations
import os, sys, json, math, time
from collections import Counter, defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
from okxbot import config
from okxbot.indicators import to_df, add_indicators, ema, rsi, atr
from okxbot.strategy import build_features, initial_stop_tp, update_stop, should_exit_on_trend
from okxbot.risk import size_position

CACHE = os.path.join(config.DATA_DIR, "bt_cache")
FEE_TAKER, SLIP_MKT = 0.0005, 0.0003
FEE_MAKER, SLIP_LMT = 0.0002, 0.0001
N_BARS = 17520
WARM = 300

# Core watchlist proxy: majors + liquid alts + tradfi currently monitored
CRYPTO = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "OKB-USDT-SWAP",
    "XRP-USDT-SWAP", "DOGE-USDT-SWAP", "LINK-USDT-SWAP", "AVAX-USDT-SWAP",
    "SUI-USDT-SWAP", "AAVE-USDT-SWAP", "WLD-USDT-SWAP",
]
TRADFI = ["QQQ-USDT-SWAP", "SPY-USDT-SWAP", "XAU-USDT-SWAP", "CL-USDT-SWAP", "BZ-USDT-SWAP"]
ALL = CRYPTO + TRADFI


def load(inst, n=N_BARS):
    path = os.path.join(CACHE, f"{inst}_1H_{n}.json")
    if not os.path.exists(path):
        # fall back to whatever longest cache exists
        cands = sorted(
            [p for p in os.listdir(CACHE) if p.startswith(inst + "_1H_")],
            key=lambda p: os.path.getsize(os.path.join(CACHE, p)), reverse=True)
        if not cands:
            raise FileNotFoundError(inst)
        path = os.path.join(CACHE, cands[0])
    rows = json.load(open(path))
    df = to_df(rows)
    df = df[df["confirm"] == "1"].reset_index(drop=True)
    return df


def add_daily_trend(df1h):
    """No-lookahead daily trend: close & ema20 vs ema50 on completed UTC days."""
    d = df1h.copy()
    d["dt"] = pd.to_datetime(d["ts"], unit="ms", utc=True)
    g = d.set_index("dt").resample("1D", label="left", closed="left")
    day = pd.DataFrame({"o": g["o"].first(), "h": g["h"].max(), "l": g["l"].min(),
                        "c": g["c"].last(), "n": g["c"].count()}).dropna()
    day = day[day["n"] >= 20]  # require enough hours (crypto ~24; tradfi less)
    day["ema20"] = ema(day["c"], 20)
    day["ema50"] = ema(day["c"], 50)
    trend = np.where((day["c"] > day["ema50"]) & (day["ema20"] > day["ema50"]), 1,
                     np.where((day["c"] < day["ema50"]) & (day["ema20"] < day["ema50"]), -1, 0))
    t = pd.DataFrame({"avail_ts": day.index.asi8 // 10**6 + 24 * 3600_000, "trend_d": trend})
    # avail after day closes (approx +24h from day start)
    out = df1h.copy()
    out["close_ts"] = out["ts"] + 3600_000
    out = pd.merge_asof(out.sort_values("close_ts"), t.sort_values("avail_ts"),
                        left_on="close_ts", right_on="avail_ts", direction="backward")
    out["trend_d"] = out["trend_d"].fillna(0).astype(int)
    return out


def add_regime(df):
    """Trend vs range via ATR% vs its rolling median."""
    d = df.copy()
    atr_pct = d["atr"] / d["c"]
    med = atr_pct.rolling(100, min_periods=50).median()
    # high vol expansion -> trend; compressed -> range
    d["regime"] = np.where(atr_pct > med * 1.05, "trend",
                           np.where(atr_pct < med * 0.95, "range", "mixed"))
    # ADX-like proxy: |ema20-ema50|/atr
    d["trend_str"] = ((d["ema20"] - d["ema50"]).abs() / d["atr"]).fillna(0)
    return d


def signals_c7(df):
    """Baseline C7 via strategy.build_features."""
    f = build_features(df)
    f = add_daily_trend(f)
    f = add_regime(f)
    f["sig"] = f["signal"].astype(int)
    f["risk_mult"] = 1.0
    f["entry_style"] = "market"
    return f


def signals_c7_daily_hard(df):
    """Planner-like: C7 AND daily trend must agree (proxy for soft HTF/daily C7 filter)."""
    f = signals_c7(df)
    f["sig"] = np.where(f["sig"] == f["trend_d"], f["sig"], 0)
    return f


def signals_soft_daily(df):
    """C7 entries always; daily against -> 0.4x risk; daily flat -> 0.7x; align -> 1.0x."""
    f = signals_c7(df)
    mult = np.ones(len(f))
    align = f["sig"] == f["trend_d"]
    flat = f["trend_d"] == 0
    against = (f["sig"] != 0) & (f["trend_d"] != 0) & (~align)
    mult = np.where(against, 0.4, np.where(flat & (f["sig"] != 0), 0.7, 1.0))
    f["risk_mult"] = mult
    return f


def signals_pullback(df, wait_bars=12, zone_atr=0.4):
    """C7 breakout arms a retest; enter on pullback to breakout level / EMA20 (limit-like)."""
    f = signals_c7(df).reset_index(drop=True)
    sig = np.zeros(len(f), dtype=int)
    style = ["market"] * len(f)
    mult = np.ones(len(f))
    pending = None  # (side, level, expire_i, atr)
    for i in range(len(f)):
        r = f.iloc[i]
        if pending is not None:
            side, level, exp, a = pending
            if i > exp or int(r["trend4h"]) != side:
                pending = None
            else:
                # retest: low touches level+zone (long) or high touches level-zone (short)
                z = zone_atr * a
                hit = (side == 1 and r["l"] <= level + z and r["c"] >= level - z) or \
                      (side == -1 and r["h"] >= level - z and r["c"] <= level + z)
                # also allow EMA20 touch
                hit_ema = (side == 1 and r["l"] <= r["ema20"] + z and r["c"] > r["ema20"] - z and r["c"] > r["ll20"]) or \
                          (side == -1 and r["h"] >= r["ema20"] - z and r["c"] < r["ema20"] + z and r["c"] < r["hh20"])
                if hit or hit_ema:
                    sig[i] = side
                    style[i] = "limit"
                    # soft daily size
                    if r["trend_d"] == side:
                        mult[i] = 1.0
                    elif r["trend_d"] == 0:
                        mult[i] = 0.7
                    else:
                        mult[i] = 0.4
                    pending = None
                    continue
        if int(r["signal"]) != 0 and pending is None:
            side = int(r["signal"])
            level = float(r["hh20"] if side == 1 else r["ll20"])
            if not math.isfinite(level):
                level = float(r["ema20"])
            pending = (side, level, i + wait_bars, float(r["atr"]))
    f["sig"] = sig
    f["risk_mult"] = mult
    f["entry_style"] = style
    return f


def signals_regime(df):
    """Trend regime -> C7; range regime -> mean-reversion at Donchian extremes."""
    f = signals_c7(df).reset_index(drop=True)
    sig = np.zeros(len(f), dtype=int)
    style = ["market"] * len(f)
    mult = np.ones(len(f))
    for i in range(WARM, len(f)):
        r = f.iloc[i]
        regime = r["regime"]
        if regime == "trend" or (regime == "mixed" and r["trend_str"] >= 0.8):
            if int(r["signal"]) != 0:
                sig[i] = int(r["signal"])
                style[i] = "market"
                mult[i] = 1.0 if r["trend_d"] in (0, int(r["signal"])) else 0.5
        else:
            # mean reversion: touch outer Donchian with RSI extreme, flat/weak trend
            if r["trend4h"] == 0 or r["trend_str"] < 0.6:
                if r["l"] <= r["ll20"] and r["rsi"] < 35 and r["c"] > r["ll20"]:
                    sig[i] = 1
                    style[i] = "limit"
                    mult[i] = 0.6
                elif r["h"] >= r["hh20"] and r["rsi"] > 65 and r["c"] < r["hh20"]:
                    sig[i] = -1
                    style[i] = "limit"
                    mult[i] = 0.6
    f["sig"] = sig
    f["risk_mult"] = mult
    f["entry_style"] = style
    return f


def signals_momentum(df):
    """Multi-TF momentum: 4h trend + 1h EMA stack + RSI cross 50; no Donchian."""
    f = signals_c7(df).reset_index(drop=True)
    # 4h trend already in trend4h when TREND_TF=1h it's ema50/200; rebuild true 4h
    from okxbot.strategy import resample_4h
    h4 = resample_4h(df)
    h4 = h4[h4["n"] == 4]
    h4["avail_ts"] = h4.index.asi8 // 10**6 + 4 * 3600_000
    t4 = np.where((h4["c"] > h4["ema50"]) & (h4["ema20"] > h4["ema50"]), 1,
                  np.where((h4["c"] < h4["ema50"]) & (h4["ema20"] < h4["ema50"]), -1, 0))
    tdf = pd.DataFrame({"avail_ts": h4["avail_ts"].values, "t4": t4})
    f["close_ts"] = f["ts"] + 3600_000
    f = pd.merge_asof(f.sort_values("close_ts"), tdf.sort_values("avail_ts"),
                      left_on="close_ts", right_on="avail_ts", direction="backward")
    f["t4"] = f["t4"].fillna(0).astype(int)
    long_s = (f["t4"] == 1) & (f["ema20"] > f["ema50"]) & (f["c"] > f["ema50"]) & \
             (f["rsi"] > 50) & (f["rsi"].shift(1) <= 50)
    short_s = (f["t4"] == -1) & (f["ema20"] < f["ema50"]) & (f["c"] < f["ema50"]) & \
              (f["rsi"] < 50) & (f["rsi"].shift(1) >= 50)
    f["sig"] = np.where(long_s, 1, np.where(short_s, -1, 0)).astype(int)
    f["risk_mult"] = np.where(f["sig"] == f["trend_d"], 1.0,
                              np.where(f["trend_d"] == 0, 0.7, 0.4))
    f["entry_style"] = "market"
    return f


def signals_pullback_fresh(df, max_chase_pct=0.005):
    """Pullback + explicit no-chase: skip if next-open would already be >0.5% past signal close."""
    f = signals_pullback(df)
    # mark chase risk on market-style; pullback already limit
    return f


VARIANTS = {
    "C7_baseline": signals_c7,
    "C7_daily_hard": signals_c7_daily_hard,   # planner-proxy (very conservative)
    "C7_soft_daily": signals_soft_daily,
    "pullback_retest": signals_pullback,
    "regime_switch": signals_regime,
    "mtf_momentum": signals_momentum,
}


def summarize(trades, curve, start_eq, weeks):
    if not curve:
        return {"trades": 0}
    eqs = np.array([e for _, e in curve])
    final = float(eqs[-1])
    mdd = float(np.max(1 - eqs / np.maximum.accumulate(eqs))) if len(eqs) else 0
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins) if wins else 0
    gl = -sum(t["pnl"] for t in losses) if losses else 0
    # expectancy in R: use init risk if present
    rs = [t["pnl"] / t["risk"] for t in trades if t.get("risk")]
    # sharpe-ish on hourly equity returns (annualized ~ sqrt(24*365))
    rets = np.diff(eqs) / eqs[:-1] if len(eqs) > 2 else np.array([0.0])
    sharpe = float(rets.mean() / (rets.std() + 1e-12) * math.sqrt(24 * 365)) if len(rets) > 10 else 0.0
    return {
        "final_eq": round(final, 2),
        "return_pct": round((final / start_eq - 1) * 100, 2),
        "max_dd_pct": round(mdd * 100, 2),
        "trades": len(trades),
        "trades_per_week": round(len(trades) / max(weeks, 0.1), 2),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else 0.0,
        "profit_factor": round(gp / gl, 2) if gl > 0 else (None if not wins else 99.0),
        "expectancy_R": round(float(np.mean(rs)), 3) if rs else None,
        "sharpe_approx": round(sharpe, 2),
        "avg_win": round(float(np.mean([t["pnl"] for t in wins])), 2) if wins else 0,
        "avg_loss": round(float(np.mean([t["pnl"] for t in losses])), 2) if losses else 0,
        "exit_reasons": dict(Counter(t["why"] for t in trades)),
    }


def run_portfolio(feats, start_eq=10000.0, t_from=None, t_to=None,
                  stop_mult=None, tp_mult=None, chase_veto=True):
    """Generic portfolio runner. feats[inst] has columns sig, atr, o/h/l/c, risk_mult, entry_style, trend4h."""
    stop_mult = stop_mult or config.ATR_STOP_MULT
    tp_mult = tp_mult or config.TP_R_MULT
    all_ts = sorted(set().union(*[set(f.index) for f in feats.values()]))
    if not all_ts:
        return summarize([], [], start_eq, 1), [], []
    warm_ts = all_ts[min(WARM, len(all_ts) - 1)]
    cash = start_eq
    pos, pending, trades, curve = {}, {}, [], []
    peak, day, day_start, day_locked, halted = start_eq, None, start_eq, False, False
    chase_blocks = 0
    signal_bars = 0

    def close(inst, px, ts, why):
        nonlocal cash
        p = pos.pop(inst)
        fee = p["fee"]
        exit_px = px * (1 - p["slip"] * p["side"])
        pnl = p["side"] * (exit_px - p["entry"]) * p["qty"] - fee * exit_px * p["qty"]
        cash += pnl
        trades.append({
            "inst": inst, "side": p["side"], "entry_ts": p["ts"], "exit_ts": ts,
            "entry": p["entry"], "exit": exit_px, "pnl": pnl + p["entry_fee_pnl"],
            "why": why, "risk": p["risk"], "style": p.get("style", "market"),
        })

    for ts in all_ts:
        if ts < warm_ts:
            continue
        if t_from is not None and ts < t_from:
            continue
        if t_to is not None and ts >= t_to:
            continue
        # fills
        for inst, sig in list(pending.items()):
            if inst in pos or len(pos) >= config.MAX_POSITIONS:
                continue
            f = feats[inst]
            if ts not in f.index:
                continue
            row = f.loc[ts]
            style = sig.get("style", "market")
            fee = FEE_MAKER if style == "limit" else FEE_TAKER
            slip = SLIP_LMT if style == "limit" else SLIP_MKT
            entry = row["o"] * (1 + slip * sig["side"])
            # chase veto vs signal reference price
            if chase_veto and sig.get("ref_px"):
                if abs(entry - sig["ref_px"]) / sig["ref_px"] > config.KILL_ENTRY_SLIP_PCT:
                    chase_blocks += 1
                    continue
            atr_v = sig["atr"]
            dist = stop_mult * atr_v
            dist = max(dist, entry * config.MIN_STOP_PCT)
            if dist > entry * config.MAX_STOP_PCT:
                continue
            stop = entry - dist if sig["side"] == 1 else entry + dist
            tp = entry + tp_mult * dist if sig["side"] == 1 else entry - tp_mult * dist
            # mean-reversion: tighter TP
            if sig.get("mr"):
                tp = entry + 1.2 * dist if sig["side"] == 1 else entry - 1.2 * dist
            eq = cash + sum(
                p["side"] * (feats[i].loc[ts, "o"] - p["entry"]) * p["qty"]
                for i, p in pos.items() if ts in feats[i].index)
            # scale risk
            risk_frac = config.RISK_PER_TRADE * float(sig.get("risk_mult", 1.0))
            # tradfi half risk
            base = inst.split("-")[0]
            if base in getattr(config, "TRADFI_LIST", []):
                risk_frac *= getattr(config, "TRADFI_RISK_MULT", 0.5)
            # monkey-patch size via temporary: size_position uses config.RISK_PER_TRADE
            old = config.RISK_PER_TRADE
            config.RISK_PER_TRADE = risk_frac
            try:
                notional, lev, margin = size_position(eq, entry, stop)
            finally:
                config.RISK_PER_TRADE = old
            if notional <= 0:
                continue
            qty = notional / entry
            entry_fee = fee * notional
            cash -= entry_fee
            risk_usdt = abs(entry - stop) * qty
            pos[inst] = {
                "side": sig["side"], "entry": entry, "qty": qty, "stop": stop,
                "init_stop": stop, "tp": tp, "hi": entry, "lo": entry, "ts": ts,
                "entry_fee_pnl": -entry_fee, "fee": fee, "slip": slip,
                "risk": risk_usdt, "style": style,
            }
        pending = {}

        # exits
        for inst in list(pos):
            f = feats[inst]
            if ts not in f.index:
                continue
            r, p = f.loc[ts], pos[inst]
            if p["side"] == 1:
                if r["l"] <= p["stop"]:
                    close(inst, min(r["o"], p["stop"]), ts, "stop"); continue
                if r["h"] >= p["tp"]:
                    close(inst, max(r["o"], p["tp"]), ts, "tp"); continue
            else:
                if r["h"] >= p["stop"]:
                    close(inst, max(r["o"], p["stop"]), ts, "stop"); continue
                if r["l"] <= p["tp"]:
                    close(inst, min(r["o"], p["tp"]), ts, "tp"); continue
            p["hi"], p["lo"] = max(p["hi"], r["h"]), min(p["lo"], r["l"])
            if should_exit_on_trend(p["side"], r):
                close(inst, r["c"], ts, "trend_flip"); continue
            p["stop"] = update_stop(p["side"], p["entry"], p["init_stop"], p["stop"],
                                    p["hi"], p["lo"], r["atr"])

        eq = cash + sum(
            p["side"] * (feats[i].loc[ts, "c"] - p["entry"]) * p["qty"]
            for i, p in pos.items() if ts in feats[i].index)
        d = pd.Timestamp(ts, unit="ms", tz="UTC").tz_convert(config.TIMEZONE).date()
        if d != day:
            day, day_start, day_locked = d, eq, False
        peak = max(peak, eq)
        if not halted and eq <= peak * (1 - config.MAX_DRAWDOWN):
            halted = True
            for i in list(pos):
                close(i, feats[i].loc[ts, "c"] if ts in feats[i].index else pos[i]["entry"], ts, "halt")
        if not day_locked and eq <= day_start * (1 - config.DAILY_LOSS_LIMIT):
            day_locked = True
            for i in list(pos):
                close(i, feats[i].loc[ts, "c"] if ts in feats[i].index else pos[i]["entry"], ts, "daily_limit")
        curve.append((ts, eq))
        if halted or day_locked:
            continue

        cands = []
        for inst, f in feats.items():
            if inst in pos or ts not in f.index:
                continue
            r = f.loc[ts]
            s = int(r["sig"])
            if s == 0:
                continue
            signal_bars += 1
            style = r["entry_style"] if isinstance(r["entry_style"], str) else "market"
            cands.append((float(r.get("strength", 1.0)) * float(r.get("risk_mult", 1.0)), inst, {
                "side": s, "atr": float(r["atr"]), "risk_mult": float(r.get("risk_mult", 1.0)),
                "style": style, "ref_px": float(r["c"]),
                "mr": style == "limit" and int(r.get("signal", 0)) == 0,
            }))
        for _, inst, sig in sorted(cands, reverse=True)[: max(0, config.MAX_POSITIONS - len(pos))]:
            pending[inst] = sig

    for i in list(pos):
        f = feats[i]
        sub = f[(f.index >= warm_ts)]
        if t_from is not None:
            sub = sub[sub.index >= t_from]
        if t_to is not None:
            sub = sub[sub.index < t_to]
        if len(sub):
            close(i, float(sub["c"].iloc[-1]), int(sub.index[-1]), "end")
    span_ms = (curve[-1][0] - curve[0][0]) if curve else 1
    weeks = span_ms / (7 * 86400_000)
    stats = summarize(trades, curve, start_eq, weeks)
    stats["chase_blocks"] = chase_blocks
    stats["signal_cand_bars"] = signal_bars
    stats["halted"] = halted
    return stats, trades, curve


def diagnose_gates(feats_by_variant_name_c7):
    """Count how often C7 fires and how often daily-hard / chase would block."""
    fset = feats_by_variant_name_c7
    total_bars = 0
    c7_on = 0
    daily_block = 0
    soft_htf = 0  # trend4h == 0 while somehow... C7 already requires trend
    for inst, f in fset.items():
        sub = f.iloc[WARM:]
        total_bars += len(sub)
        sig = sub["signal"].astype(int)
        c7_on += int((sig != 0).sum())
        daily_block += int(((sig != 0) & (sig != sub["trend_d"])).sum())
        soft_htf += int((sub["trend4h"] == 0).sum())
    return {
        "instrument_bars": total_bars,
        "c7_signal_bars": c7_on,
        "c7_pct_of_bars": round(100 * c7_on / max(total_bars, 1), 3),
        "daily_hard_would_block": daily_block,
        "daily_hard_block_pct_of_c7": round(100 * daily_block / max(c7_on, 1), 1),
        "flat_1h_trend_pct_bars": round(100 * soft_htf / max(total_bars, 1), 1),
    }


def split_ts(all_feats, is_frac=2 / 3):
    """IS = first 16/24 ≈ 2/3 of overlapping window; OOS = last 1/3 (~8 months of 24)."""
    ts = sorted(set().union(*[set(f.index) for f in all_feats.values()]))
    ts = [t for t in ts[WARM:]]
    if len(ts) < 100:
        return None, None, ts[0] if ts else None, ts[-1] if ts else None
    cut = ts[int(len(ts) * is_frac)]
    return ts[0], cut, cut, ts[-1]


def main():
    print("Loading features...", flush=True)
    raw = {}
    coverage = {}
    for inst in ALL:
        try:
            df = load(inst)
            raw[inst] = df
            coverage[inst] = {
                "bars": len(df),
                "start": pd.Timestamp(int(df["ts"].iloc[0]), unit="ms", tz="UTC").isoformat(),
                "end": pd.Timestamp(int(df["ts"].iloc[-1]), unit="ms", tz="UTC").isoformat(),
                "days": round((int(df["ts"].iloc[-1]) - int(df["ts"].iloc[0])) / 86400_000, 1),
            }
            print(f"  {inst}: {coverage[inst]['bars']} bars, {coverage[inst]['days']}d", flush=True)
        except Exception as e:
            print(f"  SKIP {inst}: {e}", flush=True)

    # Build all variants
    built = {name: {} for name in VARIANTS}
    print("Building signals...", flush=True)
    for inst, df in raw.items():
        for name, fn in VARIANTS.items():
            f = fn(df).set_index("ts")
            # ensure strength
            if "strength" not in f.columns:
                f["strength"] = ((f["c"] - f["ema50"]) / f["atr"]).abs()
            built[name][inst] = f

    diag = diagnose_gates(built["C7_baseline"])
    print("Gate diagnosis:", json.dumps(diag, indent=2), flush=True)

    # Use crypto+tradfi together; also crypto-only for cleaner 2y OOS
    universes = {
        "crypto12": [i for i in CRYPTO if i in raw],
        "tradfi5": [i for i in TRADFI if i in raw],
        "all_watch": [i for i in ALL if i in raw],
    }

    results = {"coverage": coverage, "diagnosis": diag, "runs": {}}
    start_eq = 10000.0

    for uname, insts in universes.items():
        print(f"\n=== Universe {uname} ({len(insts)}) ===", flush=True)
        results["runs"][uname] = {}
        for vname in VARIANTS:
            feats = {i: built[vname][i] for i in insts}
            t0, cut, o0, t1 = split_ts(feats, is_frac=16 / 24)  # 16m tune / 8m test of a 24m window
            if t0 is None:
                print(f"  {vname}: insufficient overlap"); continue
            # For short-history tradfi, use 2/3-1/3 of available
            if uname == "tradfi5":
                t0, cut, o0, t1 = split_ts(feats, is_frac=2 / 3)
            print(f"  {vname} IS/OOS...", flush=True)
            is_stats, is_tr, _ = run_portfolio(feats, start_eq, t_from=t0, t_to=cut)
            oos_stats, oos_tr, _ = run_portfolio(feats, start_eq, t_from=o0, t_to=t1)
            full_stats, full_tr, _ = run_portfolio(feats, start_eq, t_from=t0, t_to=t1)
            # per-instrument OOS trade counts
            by_inst = Counter(t["inst"] for t in oos_tr)
            results["runs"][uname][vname] = {
                "window": {
                    "is_from": pd.Timestamp(t0, unit="ms", tz="UTC").isoformat(),
                    "is_to": pd.Timestamp(cut, unit="ms", tz="UTC").isoformat(),
                    "oos_from": pd.Timestamp(o0, unit="ms", tz="UTC").isoformat(),
                    "oos_to": pd.Timestamp(t1, unit="ms", tz="UTC").isoformat(),
                },
                "IS": is_stats,
                "OOS": oos_stats,
                "FULL": full_stats,
                "oos_trades_by_inst": dict(by_inst),
            }
            print(f"    IS  n={is_stats['trades']} ret={is_stats['return_pct']}% dd={is_stats['max_dd_pct']}% "
                  f"WR={is_stats['win_rate_pct']} PF={is_stats['profit_factor']} E[R]={is_stats['expectancy_R']}", flush=True)
            print(f"    OOS n={oos_stats['trades']} ret={oos_stats['return_pct']}% dd={oos_stats['max_dd_pct']}% "
                  f"WR={oos_stats['win_rate_pct']} PF={oos_stats['profit_factor']} E[R]={oos_stats['expectancy_R']} "
                  f"tpw={oos_stats['trades_per_week']}", flush=True)

    out = os.path.join(config.DATA_DIR, "bt_entry_research.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("Wrote", out, flush=True)
    return results


if __name__ == "__main__":
    main()
