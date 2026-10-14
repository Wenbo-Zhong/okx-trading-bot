"""Portfolio backtest of the technical part (no news) on OKX public historical 1h candles.
Applies the same signals, sizing and guardrails as the live bot.
Assumptions: entry at next 1h open after a closed-bar signal; stop/TP checked intrabar
(if both touched in one bar, stop is assumed first); taker fee 0.05%/side + 0.03%/side slippage;
funding fees NOT modelled."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json, os, sys, time
import numpy as np, pandas as pd
from okxbot import config
from okxbot.okx_client import OKX
from okxbot.indicators import to_df
from okxbot.strategy import build_features, initial_stop_tp, update_stop, should_exit_on_trend
from okxbot.risk import size_position

FEE = 0.0005
SLIP = 0.0003
CACHE = os.path.join(config.DATA_DIR, "bt_cache")


def load(inst, n):
    os.makedirs(CACHE, exist_ok=True)
    f = os.path.join(CACHE, f"{inst}_1H_{n}.json")
    if os.path.exists(f) and time.time() - os.path.getmtime(f) < 86400:
        rows = json.load(open(f))
    else:
        rows = OKX().history_candles(inst, "1H", n)
        json.dump(rows, open(f, "w"))
    df = to_df(rows)
    return df[df["confirm"] == "1"].reset_index(drop=True)


def load_funding(inst, fill):
    """{ts: rate} for settled funding at bar timestamps. fill=True: before the first available record
    (OKX only serves ~3 months), assume the instrument's mean rate every 8h (00/08/16 UTC)."""
    from okxbot import funding as F
    h = F.history(inst)
    if not h:
        return {}, None
    d = {ts: r for ts, r in h}
    first = h[0][0]
    if fill:
        mean = sum(r for _, r in h) / len(h)
        t = first - 8 * 3600_000
        while t > first - 400 * 86400_000:
            d[t] = mean
            t -= 8 * 3600_000
    return d, first


def run(insts, n_bars, start_eq=500.0, verbose=False, t_from=None, t_to=None, funding_mode=None, funding_filter=False, stop_hook=None, time_stop=None, spot_pref=False, harvest=False, flip_signals=False):
    """funding_mode: None (ignore), "real" (only real settled history), "fill" (real + mean-rate fill before history).
    flip_signals: invert every non-zero entry signal and the trend filter used for trend_flip exits,
    then recompute stops/TP/sizing for the flipped side (anti-C7 offline experiment; default False)."""
    feats = {i: build_features(load(i, n_bars)).set_index("ts") for i in insts}
    if flip_signals:
        for f in feats.values():
            f["signal"] = -f["signal"]
            f["trend4h"] = -f["trend4h"]
    all_ts = sorted(set().union(*[f.index for f in feats.values()]))
    warm = all_ts[300] if len(all_ts) > 300 else all_ts[0]
    cash = start_eq
    pos = {}       # inst -> dict
    trades = []
    curve = []
    peak = start_eq
    day, day_start, day_locked, halted = None, start_eq, False, False
    pending = {}   # inst -> signal row to enter at next open
    fund, fund_paid = {}, 0.0
    if funding_mode:
        for i in insts:
            fund[i], _ = load_funding(i, funding_mode == "fill")
    fund_ts_sorted = {i: sorted(v) for i, v in fund.items()}

    def last_rate_8h(inst, ts):
        """Latest settled rate at or before ts, as 8h-equivalent (interval from the gap to the previous record)."""
        import bisect
        arr = fund_ts_sorted.get(inst) or []
        k = bisect.bisect_right(arr, ts) - 1
        if k < 1:
            return None
        gap_h = (arr[k] - arr[k - 1]) / 3.6e6 or 8
        return fund[inst][arr[k]] * 8 / gap_h

    def close(inst, px, ts, why):
        nonlocal cash
        p = pos.pop(inst)
        exit_px = px * (1 - SLIP * p["side"])
        pnl = p["side"] * (exit_px - p["entry"]) * p["qty"] - p.get("fee_rate", FEE) * exit_px * p["qty"]
        cash += pnl
        trades.append({"inst": inst, "side": p["side"], "entry_ts": p["ts"], "exit_ts": ts,
                       "entry": p["entry"], "exit": exit_px, "pnl": pnl + p["entry_fee_pnl"],
                       "why": why, "lev": p["lev"], "venue": p.get("venue", "swap"), "intent": p.get("intent", "trend"),
                       "funding_pnl": p.get("fund", 0.0)})

    for ts in all_ts:
        if ts < warm or (t_from is not None and ts < t_from) or (t_to is not None and ts >= t_to):
            continue
        # ---- funding settled at this bar's open time, paid/received by positions opened earlier
        if funding_mode:
            for inst, p in pos.items():
                r = fund.get(inst, {}).get(ts)
                if r is not None and p["ts"] < ts and ts in feats[inst].index and p.get("venue") != "spot":
                    c = p["side"] * r * p["qty"] * feats[inst].loc[ts, "o"]   # >0 = we pay
                    cash -= c; fund_paid += c; p["entry_fee_pnl"] -= c; p["fund"] = p.get("fund", 0.0) - c
        # ---- entries queued from previous bar's close, filled at this bar's open
        for inst, sig in list(pending.items()):
            if funding_filter:
                from okxbot import funding as F
                r8 = last_rate_8h(inst, ts)
                if r8 is not None and F.pays_funding(sig["side"], r8) and abs(r8) > config.FUNDING_MAX_PAY_8H:
                    continue
            f = feats[inst]
            if ts not in f.index or inst in pos or len(pos) >= config.MAX_POSITIONS:
                continue
            row = f.loc[ts]
            entry = row["o"] * (1 + SLIP * sig["side"])
            stop, tp = initial_stop_tp(sig["side"], entry, sig["atr"])
            if stop is None:
                continue
            if stop_hook is not None:   # e.g. liquidation-cluster stop nudge (TP price unchanged)
                stop = stop_hook(inst, ts, sig["side"], entry, stop, sig["atr"])
            eq = cash + sum(p["side"] * (feats[i].loc[ts, "o"] - p["entry"]) * p["qty"]
                            for i, p in pos.items() if ts in feats[i].index)
            notional, lev, margin = size_position(eq, entry, stop)
            qty = notional / entry
            # spot_pref: ~1x longs go to the spot pair (spot taker fee, no funding); swap candles used as price proxy
            venue = "spot" if (spot_pref and sig["side"] == 1 and lev <= config.SPOT_PREFER_MAX_LEV) else "swap"
            intent = "trend"
            if venue == "spot" and harvest:   # funding-harvest exception: last settled 8h rate <= -threshold
                r8h = last_rate_8h(inst, ts)
                if r8h is not None and r8h <= -config.FUNDING_HARVEST_MIN_8H:
                    venue, intent = "swap", "funding_harvest"
            fee_rate = config.SPOT_FEE if venue == "spot" else FEE
            fee = fee_rate * notional
            cash -= fee
            pos[inst] = {"side": sig["side"], "entry": entry, "qty": qty, "stop": stop, "init_stop": stop,
                         "tp": tp, "hi": entry, "lo": entry, "ts": ts, "lev": lev, "entry_fee_pnl": -fee,
                         "venue": venue, "fee_rate": fee_rate, "intent": intent, "fund": 0.0}
        pending = {}
        # ---- intrabar exits
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
            if time_stop is not None:   # (N hours, X R): close at bar close if the trade never reached +X R within N h
                n_h, x_r = time_stop[:2]
                use_cur = len(time_stop) > 2 and time_stop[2] == "cur"   # "cur": unrealized R at bar close; else MFE
                risk = abs(p["entry"] - p["init_stop"])
                if use_cur:
                    mfe = p["side"] * (r["c"] - p["entry"]) / risk if risk > 0 else 0
                else:
                    mfe = ((p["hi"] - p["entry"]) if p["side"] == 1 else (p["entry"] - p["lo"])) / risk if risk > 0 else 0
                if ts + 3600_000 - p["ts"] >= n_h * 3600_000 and mfe < x_r:
                    close(inst, r["c"], ts, "time_stop"); continue
            p["stop"] = update_stop(p["side"], p["entry"], p["init_stop"], p["stop"], p["hi"], p["lo"], r["atr"])
        # ---- mark to market at close & guardrails
        eq = cash + sum(p["side"] * (feats[i].loc[ts, "c"] - p["entry"]) * p["qty"]
                        for i, p in pos.items() if ts in feats[i].index)
        d = pd.Timestamp(ts, unit="ms", tz="UTC").tz_convert(config.TIMEZONE).date()
        if d != day:
            day, day_start, day_locked = d, eq, False
        peak = max(peak, eq)
        if not halted and eq <= peak * (1 - config.MAX_DRAWDOWN):
            halted = True
            for i in list(pos): close(i, feats[i].loc[ts, "c"] if ts in feats[i].index else pos[i]["entry"], ts, "halt")
        if not day_locked and eq <= day_start * (1 - config.DAILY_LOSS_LIMIT):
            day_locked = True
            for i in list(pos): close(i, feats[i].loc[ts, "c"] if ts in feats[i].index else pos[i]["entry"], ts, "daily_limit")
        curve.append((ts, eq))
        if halted or day_locked:
            continue
        # ---- new signals (ranked by strength)
        cands = []
        for inst, f in feats.items():
            if inst in pos or ts not in f.index:
                continue
            r = f.loc[ts]
            if r["signal"] != 0:
                cands.append((r["strength"], inst, {"side": int(r["signal"]), "atr": r["atr"]}))
        for _, inst, sig in sorted(cands, reverse=True)[: max(0, config.MAX_POSITIONS - len(pos))]:
            pending[inst] = sig

    for i in list(pos):
        f = feats[i]
        if t_to is not None:
            f = f[(f.index < t_to) & (f.index >= warm)]
            if t_from is not None:
                f = f[f.index >= t_from]
            close(i, f["c"].iloc[-1], f.index[-1], "end")
        else:
            close(i, f["c"].iloc[-1], all_ts[-1], "end")
    if curve:
        curve.append((curve[-1][0], cash))
    eqs = np.array([e for _, e in curve])
    final = cash
    mdd = float(np.max(1 - eqs / np.maximum.accumulate(eqs))) if len(eqs) else 0
    wins = [t for t in trades if t["pnl"] > 0]
    return {
        "insts": insts, "start": pd.Timestamp(curve[0][0], unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M UTC"),
        "end": pd.Timestamp(curve[-1][0], unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M UTC"),
        "start_eq": start_eq, "final_eq": round(final, 2), "return_pct": round((final / start_eq - 1) * 100, 2),
        "max_dd_pct": round(mdd * 100, 2), "trades": len(trades),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else 0,
        "avg_win": round(np.mean([t["pnl"] for t in wins]), 2) if wins else 0,
        "avg_loss": round(np.mean([t["pnl"] for t in trades if t["pnl"] <= 0]), 2) if len(wins) < len(trades) else 0,
        "profit_factor": round(sum(t["pnl"] for t in wins) / -sum(t["pnl"] for t in trades if t["pnl"] <= 0), 2)
        if len(wins) < len(trades) else None,
        "halted": halted, "funding_paid": round(fund_paid, 2), "exit_reasons": pd.Series([t["why"] for t in trades]).value_counts().to_dict(),
    }, trades, curve


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8760
    insts = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP", "DOGE-USDT-SWAP", "SUI-USDT-SWAP"]
    from backtests.provenance import provenance
    metadata = provenance()
    metadata["run_args"] = {"insts": insts, "n_bars": n, "start_eq": 500.0}
    res, trades, curve = run(insts, n)
    print(json.dumps(res, indent=1))
    for i in insts:
        r, _, _ = run([i], n)
        print(i, {k: r[k] for k in ["return_pct", "max_dd_pct", "trades", "win_rate_pct", "profit_factor"]})
    csv_path = os.path.join(config.DATA_DIR, "backtest_trades.csv")
    pd.DataFrame(trades).to_csv(csv_path, index=False)
    with open(csv_path + ".meta.json", "w") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")
