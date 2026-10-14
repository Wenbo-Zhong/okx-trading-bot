"""C7 backtest on OKX stock/commodity perps (production 1h candles, all history since listing)."""
import json, sys, numpy as np, pandas as pd
import config, backtest
TRADFI = ["MU","CRCL","NVDA","QQQ","MRVL","PLTR","DELL","ORCL","MSFT","HOOD","CRWV","ASML","LLY","XAU","CL","BZ","XAG"]
INSTS = [f"{s}-USDT-SWAP" for s in TRADFI]
def session(ts):
    t = pd.Timestamp(ts, unit="ms", tz="UTC").tz_convert("America/New_York")
    if t.weekday() >= 5: return "weekend"
    m = t.hour * 60 + t.minute
    return "cash" if 9*60+30 <= m < 16*60 else "offhours"
if __name__ == "__main__":
    out = {}
    for i in INSTS:
        df = backtest.load(i, 8760)
        out[i] = len(df)
    print("bars per instrument:", out)
    split = pd.Timestamp("2026-07-01", tz="UTC").value // 10**6
    res = {}
    for label, a, b in [("FULL", None, None), ("IS<2026-07", None, split), ("OOS>=2026-07", split, None)]:
        r, tr, cv = backtest.run(INSTS, 8760, start_eq=10000, t_from=a, t_to=b)
        print(label, {k: r[k] for k in ["start", "end", "return_pct", "max_dd_pct", "trades", "win_rate_pct", "profit_factor", "exit_reasons"]})
        if label == "FULL": full = tr
    T = pd.DataFrame(full); T["R"] = T["pnl"] / (10000 * config.RISK_PER_TRADE)
    T["sess"] = T["entry_ts"].map(session)
    print("\nby entry session (pnl in R-ish units of 0.5% of 10k start):")
    print(T.groupby("sess")["R"].agg(["count", "mean", "sum"]).round(2))
    T["kind"] = T["inst"].map(lambda x: "commodity" if x.split("-")[0] in ("XAU","CL","BZ","XAG") else "stock")
    print(T.groupby("kind")["R"].agg(["count", "mean", "sum"]).round(2))
    print("\nper-instrument (standalone runs):")
    for i in INSTS:
        r, _, _ = backtest.run([i], 8760, start_eq=10000)
        print(f"  {i:16s} bars={out[i]:5d} ret={r['return_pct']:+6.1f}% mdd={r['max_dd_pct']:5.1f}% n={r['trades']:3d} win={r['win_rate_pct']:4.1f}% pf={r['profit_factor']}")
    # gap analysis: stop-through slippage on stops (exit worse than stop) by session
    T.to_csv("data/bt_tradfi_trades.csv", index=False)
