"""Offline experiment: C7 baseline vs flipped-direction signals.
Flipping negates entry side and the trend filter used for trend_flip exits, then
recomputes stop/TP/leverage/size for that side (not a naive PnL negate).
Does not change the live demo loop. Usage: python bt_reverse.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json, os, sys
import numpy as np
import pandas as pd
from okxbot import config
from backtests import backtest

U30 = json.load(open(os.path.join(config.DATA_DIR, "bt_universe30.json")))
U6 = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP", "DOGE-USDT-SWAP", "SUI-USDT-SWAP"]
split = pd.Timestamp("2026-06-01", tz="UTC").value // 10**6

# Lock C7 (current live defaults)
C7 = dict(
    DONCHIAN_N=12, ATR_STOP_MULT=3.0, TP_R_MULT=2.0, BREAKEVEN_AT_R=99,
    TRAIL_START_R=1.0, TRAIL_ATR_MULT=3.0, RISK_PER_TRADE=0.005,
    TREND_TF="1h", USE_MACD=False, REQUIRE_EMA_ALIGN=False, RSI_CAP=100,
    MIN_STOP_PCT=0.006, MAX_STOP_PCT=0.08,
)
for k, v in C7.items():
    setattr(config, k, v)


def summarize(res, trades, curve):
    pnls = np.array([t["pnl"] for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    # Approximate R: risk at entry ≈ RISK_PER_TRADE * start equity path is hard; use pnl / avg_|loss| as R proxy
    avg_loss = float(np.mean(np.abs(losses))) if len(losses) else float("nan")
    avg_r = float(np.mean(pnls / avg_loss)) if avg_loss and avg_loss > 0 else float("nan")
    days = (curve[-1][0] - curve[0][0]) / 86400_000 if curve else 1
    by_side = {}
    for s, name in [(1, "long"), (-1, "short")]:
        sub = [t for t in trades if t["side"] == s]
        if not sub:
            by_side[name] = {"n": 0}
            continue
        sp = np.array([t["pnl"] for t in sub])
        by_side[name] = {
            "n": len(sub),
            "win_pct": round(100 * (sp > 0).mean(), 1),
            "pnl": round(float(sp.sum()), 2),
        }
    return {
        **{k: res[k] for k in ["return_pct", "max_dd_pct", "trades", "win_rate_pct", "profit_factor",
                                "avg_win", "avg_loss", "halted", "exit_reasons"]},
        "total_pnl": round(float(pnls.sum()), 2),
        "expectancy": round(float(pnls.mean()), 4) if len(pnls) else 0,
        "avg_R_proxy": round(avg_r, 3) if avg_r == avg_r else None,
        "trades_per_day": round(res["trades"] / days, 2),
        "by_side": by_side,
        "start": res["start"], "end": res["end"],
    }


def run_one(label, universe, flip, t_from=None, t_to=None):
    res, trades, curve = backtest.run(
        universe, 8760, start_eq=500.0, t_from=t_from, t_to=t_to, flip_signals=flip
    )
    out = summarize(res, trades, curve)
    out["label"] = label
    out["flip"] = flip
    print(label, "flip="+str(flip), {k: out[k] for k in [
        "return_pct", "max_dd_pct", "trades", "win_rate_pct", "profit_factor",
        "total_pnl", "expectancy", "avg_R_proxy", "halted"]}, flush=True)
    print("  exits", out["exit_reasons"], "by_side", out["by_side"], flush=True)
    return out, trades


def main():
    rows = []
    # Primary: U30 FULL / IS / OOS
    for period, a, b in [("FULL", None, None), ("IS", None, split), ("OOS", split, None)]:
        for flip in (False, True):
            lab = f"C7 U30 {period}"
            out, _ = run_one(lab, U30, flip, a, b)
            rows.append({
                "variant": "C7_flip" if flip else "C7_base",
                "universe": "U30", "period": period,
                "return_pct": out["return_pct"], "max_dd_pct": out["max_dd_pct"],
                "trades": out["trades"], "win_rate_pct": out["win_rate_pct"],
                "profit_factor": out["profit_factor"], "total_pnl": out["total_pnl"],
                "expectancy": out["expectancy"], "avg_R_proxy": out["avg_R_proxy"],
                "avg_win": out["avg_win"], "avg_loss": out["avg_loss"],
                "trades_per_day": out["trades_per_day"], "halted": out["halted"],
                "exit_reasons": json.dumps(out["exit_reasons"]),
                "by_side": json.dumps(out["by_side"]),
            })
    # Secondary: U6 FULL
    for flip in (False, True):
        out, _ = run_one("C7 U6 FULL", U6, flip)
        rows.append({
            "variant": "C7_flip" if flip else "C7_base",
            "universe": "U6", "period": "FULL",
            "return_pct": out["return_pct"], "max_dd_pct": out["max_dd_pct"],
            "trades": out["trades"], "win_rate_pct": out["win_rate_pct"],
            "profit_factor": out["profit_factor"], "total_pnl": out["total_pnl"],
            "expectancy": out["expectancy"], "avg_R_proxy": out["avg_R_proxy"],
            "avg_win": out["avg_win"], "avg_loss": out["avg_loss"],
            "trades_per_day": out["trades_per_day"], "halted": out["halted"],
            "exit_reasons": json.dumps(out["exit_reasons"]),
            "by_side": json.dumps(out["by_side"]),
        })

    df = pd.DataFrame(rows)
    out_csv = os.path.join(config.DATA_DIR, "bt_reverse.csv")
    df.to_csv(out_csv, index=False)
    print("\n=== SUMMARY TABLE ===")
    print(df[["variant", "universe", "period", "return_pct", "max_dd_pct", "trades",
              "win_rate_pct", "profit_factor", "expectancy", "halted"]].to_string(index=False))
    print("wrote", out_csv)


if __name__ == "__main__":
    main()
