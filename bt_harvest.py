"""Funding-harvest exception sanity check (C7 U30, funding real + mean-filled before 2026-06-29, filter on).
Harvest decision uses the last SETTLED 8h rate at entry (no historical predicted rates available)."""
import json, pandas as pd, config, backtest
U30 = json.load(open("data/bt_universe30.json"))
split = pd.Timestamp("2026-06-01", tz="UTC").value // 10**6
real = pd.Timestamp("2026-06-29 08:00", tz="UTC").value // 10**6
for name, sp, hv in [("all swap", False, False), ("1x longs->spot", True, False), ("1x longs->spot + harvest exc.", True, True)]:
    for lab, a, b, mode in [("IS", None, split, "fill"), ("OOS", split, None, "fill"), ("FULL", None, None, "fill"), ("REAL-FUND window", real, None, "real")]:
        r, tr, cv = backtest.run(U30, 8760, start_eq=10000, t_from=a, t_to=b, funding_mode=mode, funding_filter=True, spot_pref=sp, harvest=hv)
        t = pd.DataFrame(tr); h = t[t["intent"] == "funding_harvest"]
        print(json.dumps({"case": name, "period": lab, "ret": float(r["return_pct"]), "dd": float(r["max_dd_pct"]),
            "trades": r["trades"], "pf": float(r["profit_factor"] or 0), "spot": int((t["venue"] == "spot").sum()),
            "harvest_n": len(h), "harvest_pnl_total": round(float(h["pnl"].sum()), 2),
            "harvest_funding_pnl": round(float(h["funding_pnl"].sum()), 2),
            "harvest_win": round(float((h["pnl"] > 0).mean() * 100), 1) if len(h) else None}), flush=True)
