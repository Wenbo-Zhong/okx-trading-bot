"""C7 with vs without funding costs (2026-10-10). OKX serves settled funding history only back to ~2026-06-29,
so the exact comparison uses that window; the full-year rows fill earlier periods with each instrument's mean rate."""
import json, sys
import pandas as pd
import config, backtest, funding

U30 = json.load(open("data/bt_universe30.json"))
U6 = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP", "DOGE-USDT-SWAP", "SUI-USDT-SWAP"]
first = max(funding.history(i)[0][0] for i in U30 + U6 if funding.history(i))
print("funding history common start:", pd.Timestamp(first, unit="ms", tz="UTC"))
rows = []
for uname, U in [("U30", U30), ("U6", U6)]:
    for label, a, mode, filt in [
        ("REAL-WINDOW no funding", first, None, False),
        ("REAL-WINDOW + funding (real)", first, "real", False),
        ("REAL-WINDOW + funding + entry filter", first, "real", True),
        ("FULL no funding", None, None, False),
        ("FULL + funding (real, mean-filled before)", None, "fill", False),
        ("FULL + funding + entry filter", None, "fill", True)]:
        r, t, c = backtest.run(U, 8760, t_from=a, funding_mode=mode, funding_filter=filt)
        days = (c[-1][0] - c[0][0]) / 86400_000
        row = {"universe": uname, "case": label, "start": r["start"], "end": r["end"],
               **{k: float(r[k]) if r[k] is not None and not isinstance(r[k], bool) else r[k]
                  for k in ["return_pct", "max_dd_pct", "trades", "win_rate_pct", "profit_factor", "funding_paid"]},
               "trades_per_day": round(r["trades"] / days, 2)}
        rows.append(row); print(row, flush=True)
pd.DataFrame(rows).to_csv("data/experiments_funding.csv", index=False)
