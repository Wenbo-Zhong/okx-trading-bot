"""Variant sweep, run on in-sample (first ~2/3) and out-of-sample (last ~1/3) separately.
All variants are reported, not just the best one."""
import json, sys, importlib
import pandas as pd
import config, backtest

INSTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP", "DOGE-USDT-SWAP", "SUI-USDT-SWAP"]
BASE = {"DONCHIAN_N": 20, "ATR_STOP_MULT": 2.5, "TP_R_MULT": 3.0, "BREAKEVEN_AT_R": 1.0, "TRAIL_START_R": 1.5, "TRAIL_ATR_MULT": 3.0, "RISK_PER_TRADE": 0.02}
VARIANTS = {
    "V0 base (20h breakout, 2.5ATR stop, 3R TP, BE@1R, trail@1.5R)": {},
    "V1 no BE/trail": {"BREAKEVEN_AT_R": 99, "TRAIL_START_R": 99},
    "V2 48h breakout": {"DONCHIAN_N": 48},
    "V3 48h breakout, 4ATR stop, no BE": {"DONCHIAN_N": 48, "ATR_STOP_MULT": 4.0, "BREAKEVEN_AT_R": 99, "TRAIL_START_R": 2.0, "TRAIL_ATR_MULT": 4.0},
    "V5 = V3 with 1% risk/trade": {"DONCHIAN_N": 48, "ATR_STOP_MULT": 4.0, "BREAKEVEN_AT_R": 99, "TRAIL_START_R": 2.0, "TRAIL_ATR_MULT": 4.0, "RISK_PER_TRADE": 0.01},
    "V6 = V1 with 1% risk/trade": {"BREAKEVEN_AT_R": 99, "TRAIL_START_R": 99, "RISK_PER_TRADE": 0.01},
    "V7 168h breakout, 8ATR stop, 4R TP, trail 8ATR@1.5R, 1% risk": {"DONCHIAN_N": 168, "ATR_STOP_MULT": 8.0, "TP_R_MULT": 4.0, "BREAKEVEN_AT_R": 99, "TRAIL_START_R": 1.5, "TRAIL_ATR_MULT": 8.0, "RISK_PER_TRADE": 0.01},
    "V4 96h breakout, 4ATR stop, 4R TP, trail@2R": {"DONCHIAN_N": 96, "ATR_STOP_MULT": 4.0, "TP_R_MULT": 4.0, "BREAKEVEN_AT_R": 99, "TRAIL_START_R": 2.0, "TRAIL_ATR_MULT": 4.0},
}
split = pd.Timestamp("2026-06-01", tz="UTC").value // 10**6
rows = []
ONLY = sys.argv[1:]
for name, ov in VARIANTS.items():
    if ONLY and not any(name.startswith(o) for o in ONLY): continue
    for k, v in {**BASE, **ov}.items():
        setattr(config, k, v)
    for label, a, b in [("IS 2025-11..2026-05", None, split), ("OOS 2026-06..2026-10", split, None), ("FULL", None, None)]:
        r, _, _ = backtest.run(INSTS, 8760, t_from=a, t_to=b)
        rows.append({"variant": name, "period": label, **{k: r[k] for k in ["return_pct", "max_dd_pct", "trades", "win_rate_pct", "profit_factor", "halted"]}})
        print(name[:40], label, {k: rows[-1][k] for k in list(rows[-1])[2:]}, flush=True)
pd.DataFrame(rows).to_csv("data/experiments.csv", index=False)
