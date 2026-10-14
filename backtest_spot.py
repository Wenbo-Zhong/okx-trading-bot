"""Backtest the spot hold-or-USDT rule vs buy-and-hold on OKX production 1h spot candles.
Signal on a closed 1h bar -> trade at next bar open. Costs: 0.1% fee + 0.05% slippage per side.
Starts fully IN the coin (like the demo account). Value measured in USDT."""
import json, sys
import numpy as np, pandas as pd
import config, backtest
from spot_alloc import alloc_trends, decide

COST = config.SPOT_FEE + 0.0005
WARM = 300


def run_coin(inst, rule="both", min_h=None, t_from=None, t_to=None):
    if min_h is not None:
        config.SPOT_MIN_SWITCH_H = min_h
    df = alloc_trends(backtest.load(inst, 8760))
    df = df.iloc[WARM:].reset_index(drop=True)
    if t_from: df = df[df["ts"] >= t_from].reset_index(drop=True)
    if t_to: df = df[df["ts"] < t_to].reset_index(drop=True)
    mode, qty, usdt = "IN", 1.0 / df["o"].iloc[0], 0.0     # start with 1 USDT worth of coin
    last_sw, pending, switches, in_bars = -10**9, None, 0, 0
    vals = []
    for i, r in df.iterrows():
        if pending:   # execute at this bar's open
            if pending == "OUT":
                usdt, qty, mode = qty * r["o"] * (1 - COST), 0.0, "OUT"
            else:
                qty, usdt, mode = usdt / r["o"] * (1 - COST), 0.0, "IN"
            switches += 1; last_sw = i; pending = None
        in_bars += mode == "IN"
        vals.append(qty * r["c"] + usdt)
        pending = decide(mode, int(r["t4"]), int(r["t1"]), i - last_sw, rule)
    v = np.array(vals); bh = df["c"].values / df["o"].iloc[0]
    dd = lambda a: float(np.max(1 - a / np.maximum.accumulate(a)))
    return {"inst": inst, "rule": rule, "min_h": config.SPOT_MIN_SWITCH_H,
            "start": pd.Timestamp(df["ts"].iloc[0], unit="ms", tz="UTC").strftime("%Y-%m-%d"),
            "end": pd.Timestamp(df["ts"].iloc[-1], unit="ms", tz="UTC").strftime("%Y-%m-%d"),
            "rule_ret_pct": round((v[-1] - 1) * 100, 2), "bh_ret_pct": round((bh[-1] - 1) * 100, 2),
            "rule_maxdd_pct": round(dd(v) * 100, 2), "bh_maxdd_pct": round(dd(bh) * 100, 2),
            "switches": switches, "time_in_pct": round(100 * in_bars / len(df), 1)}, v, bh


if __name__ == "__main__":
    split = pd.Timestamp("2026-06-01", tz="UTC").value // 10**6
    rows = []
    for rule, mh in [("both", 24), ("both", 0), ("4h", 24), ("1h", 24)]:
        for inst in ["BTC-USDT", "ETH-USDT", "OKB-USDT"]:
            for label, a, b in [("IS", None, split), ("OOS", split, None), ("FULL", None, None)]:
                r, _, _ = run_coin(inst, rule, mh, a, b)
                r["period"] = label; rows.append(r)
                print(r, flush=True)
    pd.DataFrame(rows).to_csv("data/backtest_spot.csv", index=False)
