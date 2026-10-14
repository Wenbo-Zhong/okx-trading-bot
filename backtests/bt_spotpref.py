"""Sanity check: C7 U30 + funding, ~1x longs routed to spot (spot fee 0.1%/side, no funding) vs all-swap.
Also checks PnL accounting: start + sum(trade pnl) == final equity."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json, pandas as pd
from okxbot import config
from backtests import backtest
U30 = json.load(open("data/bt_universe30.json"))
split = pd.Timestamp("2026-06-01", tz="UTC").value // 10**6
for sp in [False, True]:
    for lab, a, b in [("IS", None, split), ("OOS", split, None), ("FULL", None, None)]:
        r, tr, cv = backtest.run(U30, 8760, start_eq=10000, t_from=a, t_to=b, funding_mode="fill", funding_filter=True, spot_pref=sp)
        t = pd.DataFrame(tr)
        acct = 10000 + t["pnl"].sum() - cv[-1][1]
        v = t["venue"].value_counts().to_dict()
        lev1_long = int(((t["side"] == 1) & (t["lev"] == 1)).sum())
        print(json.dumps({"spot_pref": sp, "period": lab, "ret": float(r["return_pct"]), "dd": float(r["max_dd_pct"]),
                          "trades": r["trades"], "pf": float(r["profit_factor"] or 0), "funding": r["funding_paid"],
                          "venues": v, "lev1_longs": lev1_long, "accounting_gap": round(float(acct), 6)}), flush=True)
