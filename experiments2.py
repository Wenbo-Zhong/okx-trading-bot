"""Round 2 (2026-10-09): looser entry variants for more trade frequency.
Run on the 30-instrument SWAP universe (current top-30, so survivorship bias applies) and on the
original 6 pairs. IS = before 2026-06-01, OOS = from 2026-06-01. All variants reported."""
import json, sys
import pandas as pd
import config, backtest

U30 = json.load(open("data/bt_universe30.json"))
U6 = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP", "DOGE-USDT-SWAP", "SUI-USDT-SWAP"]
COMMON = {"BREAKEVEN_AT_R": 99, "MIN_STOP_PCT": 0.006, "MAX_STOP_PCT": 0.08}
V7 = {"DONCHIAN_N": 168, "ATR_STOP_MULT": 8.0, "TP_R_MULT": 4.0, "TRAIL_START_R": 1.5, "TRAIL_ATR_MULT": 8.0,
      "RISK_PER_TRADE": 0.01, "TREND_TF": "4h", "USE_MACD": True, "RSI_CAP": 75, "REQUIRE_EMA_ALIGN": True}
VARIANTS = {
    "V7 (current)": V7,
    "C1 48h, 4h trend, MACD+RSI, 4ATR stop, 3R, trail 4ATR@2R, 0.75%":
        {**V7, "DONCHIAN_N": 48, "ATR_STOP_MULT": 4.0, "TP_R_MULT": 3.0, "TRAIL_START_R": 2.0, "TRAIL_ATR_MULT": 4.0, "RISK_PER_TRADE": 0.0075},
    "C2 24h, 4h trend, no MACD, RSI80, 4ATR, 3R, trail 4ATR@2R, 0.5%":
        {**V7, "DONCHIAN_N": 24, "ATR_STOP_MULT": 4.0, "TP_R_MULT": 3.0, "TRAIL_START_R": 2.0, "TRAIL_ATR_MULT": 4.0, "RISK_PER_TRADE": 0.005, "USE_MACD": False, "RSI_CAP": 80},
    "C3 24h, 1h trend, no MACD/align, 4ATR, 3R, trail 4ATR@2R, 0.5%":
        {**V7, "DONCHIAN_N": 24, "ATR_STOP_MULT": 4.0, "TP_R_MULT": 3.0, "TRAIL_START_R": 2.0, "TRAIL_ATR_MULT": 4.0, "RISK_PER_TRADE": 0.005, "TREND_TF": "1h", "USE_MACD": False, "REQUIRE_EMA_ALIGN": False, "RSI_CAP": 100},
    "C4 48h, 1h trend, MACD, 6ATR, 3R, trail 6ATR@1.5R, 0.75%":
        {**V7, "DONCHIAN_N": 48, "ATR_STOP_MULT": 6.0, "TP_R_MULT": 3.0, "TRAIL_START_R": 1.5, "TRAIL_ATR_MULT": 6.0, "RISK_PER_TRADE": 0.0075, "TREND_TF": "1h"},
    "C5 24h, 4h trend, MACD, 6ATR, 3R, trail 6ATR@1.5R, 0.5%":
        {**V7, "DONCHIAN_N": 24, "ATR_STOP_MULT": 6.0, "TP_R_MULT": 3.0, "TRAIL_START_R": 1.5, "TRAIL_ATR_MULT": 6.0, "RISK_PER_TRADE": 0.005},
    "C6 72h, 4h trend, MACD, 6ATR, 4R, trail 6ATR@1.5R, 0.75%":
        {**V7, "DONCHIAN_N": 72, "ATR_STOP_MULT": 6.0, "TP_R_MULT": 4.0, "TRAIL_START_R": 1.5, "TRAIL_ATR_MULT": 6.0, "RISK_PER_TRADE": 0.0075},
    "C7 12h, 1h trend, no MACD/align, 3ATR, 2R, trail 3ATR@1R, 0.5%":
        {**V7, "DONCHIAN_N": 12, "ATR_STOP_MULT": 3.0, "TP_R_MULT": 2.0, "TRAIL_START_R": 1.0, "TRAIL_ATR_MULT": 3.0, "RISK_PER_TRADE": 0.005, "TREND_TF": "1h", "USE_MACD": False, "REQUIRE_EMA_ALIGN": False, "RSI_CAP": 100},
    "C8 24h, 4h trend, no MACD, RSI80, 3ATR, 2R, trail 3ATR@1R, 0.5%":
        {**V7, "DONCHIAN_N": 24, "ATR_STOP_MULT": 3.0, "TP_R_MULT": 2.0, "TRAIL_START_R": 1.0, "TRAIL_ATR_MULT": 3.0, "RISK_PER_TRADE": 0.005, "USE_MACD": False, "RSI_CAP": 80},
}
split = pd.Timestamp("2026-06-01", tz="UTC").value // 10**6
import os
rows = pd.read_csv("data/experiments2.csv").to_dict("records") if (sys.argv[1:] and os.path.exists("data/experiments2.csv")) else []
ONLY = sys.argv[1:]
for name, ov in VARIANTS.items():
    if ONLY and not any(name.startswith(o) for o in ONLY):
        continue
    for k, v in {**COMMON, **ov}.items():
        setattr(config, k, v)
    for uname, U in [("U30", U30), ("U6", U6)]:
        for label, a, b in [("IS", None, split), ("OOS", split, None), ("FULL", None, None)]:
            r, trades, curve = backtest.run(U, 8760, t_from=a, t_to=b)
            days = (curve[-1][0] - curve[0][0]) / 86400_000 if curve else 1
            row = {"variant": name, "universe": uname, "period": label,
                   **{k: r[k] for k in ["return_pct", "max_dd_pct", "trades", "win_rate_pct", "profit_factor", "halted"]},
                   "trades_per_day": round(r["trades"] / days, 2), "days": round(days, 1)}
            rows.append(row)
            print(name[:3], uname, label, {k: row[k] for k in list(row)[3:]}, flush=True)
    pd.DataFrame(rows).to_csv("data/experiments2.csv", index=False)
