"""Time-stop ('not working' exit) and MAX_POSITIONS=4 variants on C7, U30, funding (real + mean-filled) + entry
filter, same costs. IS < 2026-06-01 <= OOS. Usage: python3 experiments_timestop.py <shard> <nshards>"""
import json, sys, itertools
import pandas as pd
import config, backtest
U30 = json.load(open("data/bt_universe30.json"))
split = pd.Timestamp("2026-06-01", tz="UTC").value // 10**6
MODE = sys.argv[3] if len(sys.argv) > 3 else "mfe"
V = [] + [(f"TS N={n} X={x}", (n, x, MODE), 3) for n, x in itertools.product([24, 36, 48, 72], [0, 0.5, 1])] \
    + [(f"MAXPOS=4 + TS N={n} X={x}", (n, x, MODE), 4) for n, x in itertools.product([24, 36, 48, 72], [0, 0.5, 1])]
shard, ns = int(sys.argv[1]), int(sys.argv[2])
out = []
for k, (name, ts_, mp) in enumerate(V):
    if k % ns != shard:
        continue
    config.MAX_POSITIONS = mp
    for lab, a, b in [("IS", None, split), ("OOS", split, None), ("FULL", None, None)]:
        r, tr, cv = backtest.run(U30, 8760, start_eq=10000, t_from=a, t_to=b, funding_mode="fill", funding_filter=True, time_stop=ts_)
        row = {"variant": name, "period": lab, "return_pct": float(r["return_pct"]), "max_dd_pct": float(r["max_dd_pct"]),
               "trades": r["trades"], "pf": float(r["profit_factor"] or 0), "win": float(r["win_rate_pct"]),
               "time_stops": r["exit_reasons"].get("time_stop", 0)}
        out.append(row); print(json.dumps(row), flush=True)
json.dump(out, open(f"data/ts_{MODE}_shard{shard}.json", "w"))
