"""Does nudging C7 stops beyond estimated liquidation clusters help? Limited by OKX OI history (~60 days).
Map warm-up 14 days -> test window ~46 days on the 30-instrument universe. Same signals/sizing; TP price unchanged."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json, os, numpy as np, pandas as pd
from okxbot import config
from backtests import backtest
from okxbot import liqmap
U30 = json.load(open("data/bt_universe30.json"))
CACHE = "data/oi_cache"; os.makedirs(CACHE, exist_ok=True)
OI = {}
for i in U30:
    f = f"{CACHE}/{i}.json"
    if os.path.exists(f):
        OI[i] = {int(k): v for k, v in json.load(open(f)).items()}
    else:
        OI[i] = liqmap.oi_history(i, pages=15); json.dump(OI[i], open(f, "w"))
first = max(min(v) for v in OI.values() if v)
t_from = first + 14 * 86400_000
print("OI from", pd.Timestamp(first, unit="ms", tz="UTC"), "test from", pd.Timestamp(t_from, unit="ms", tz="UTC"))
BARS = {i: backtest.load(i, 8760) for i in U30}
stats = {"checked": 0, "nudged": 0, "kept": 0, "extra_pct": []}

def hook(inst, ts, side, entry, stop, atr):
    df = BARS[inst]; d = df[(df["ts"] < ts) & (df["ts"] >= ts - 720 * 3600_000)]
    bars = list(zip(d["ts"].astype(int), d["o"], d["h"], d["l"], d["c"]))
    lv = liqmap.build_levels(bars, OI.get(inst, {}))
    new, info = liqmap.adjust_stop(side, entry, stop, atr, lv)
    stats["checked"] += 1
    if info and info.get("kept_original"): stats["kept"] += 1
    if new != stop:
        stats["nudged"] += 1; stats["extra_pct"].append(abs(new - stop) / entry * 100)
    return new

rows = []
for name, hk in [("C7 baseline", None), ("C7 + liq-cluster stop nudge", hook)]:
    r, tr, cv = backtest.run(U30, 8760, start_eq=10000, t_from=t_from, stop_hook=hk)
    rows.append((name, r))
    print(name, {k: r[k] for k in ["start", "end", "return_pct", "max_dd_pct", "trades", "win_rate_pct", "profit_factor", "exit_reasons"]}, flush=True)
print("nudge stats: entries checked", stats["checked"], "nudged", stats["nudged"], "kept-original (would exceed MAX_STOP)", stats["kept"],
      "median extra stop distance %.2f%%" % (np.median(stats["extra_pct"]) if stats["extra_pct"] else 0))
