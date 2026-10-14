"""Spot allocation rule for held coins (BTC, ETH, OKB ...): hold the coin or park it in USDT.

Trend inputs (closed 1h candles only, no look-ahead):
  t4 = 4h trend: +1 if 4h close > EMA50(4h) and EMA20(4h) > EMA50(4h); -1 if both below; else 0
  t1 = 1h trend: +1 if close > EMA200(1h) and EMA50(1h) > EMA200(1h); -1 if both below; else 0
Rule with hysteresis:
  IN  -> OUT (sell to USDT) only when t4 == -1 AND t1 == -1   (confirmed downtrend)
  OUT -> IN  (rebuy)        only when t4 == +1 AND t1 == +1   (confirmed uptrend)
  anything mixed -> keep current state; plus a minimum of SPOT_MIN_SWITCH_H hours between switches."""
import numpy as np
import pandas as pd
from okxbot import config
from okxbot.indicators import ema
from okxbot.strategy import resample_4h


def alloc_trends(df1h):
    df = df1h.copy().reset_index(drop=True)
    df["ema50"] = ema(df["c"], 50)
    df["ema200"] = ema(df["c"], 200)
    df["t1"] = np.where((df["c"] > df["ema200"]) & (df["ema50"] > df["ema200"]), 1,
                        np.where((df["c"] < df["ema200"]) & (df["ema50"] < df["ema200"]), -1, 0))
    h4 = resample_4h(df1h)
    h4 = h4[h4["n"] == 4]
    h4["avail_ts"] = h4.index.as_unit("ms").asi8 + 4 * 3600_000
    t4 = np.where((h4["c"] > h4["ema50"]) & (h4["ema20"] > h4["ema50"]), 1,
                  np.where((h4["c"] < h4["ema50"]) & (h4["ema20"] < h4["ema50"]), -1, 0))
    t = pd.DataFrame({"avail_ts": h4["avail_ts"].values, "t4": t4})
    df["close_ts"] = df["ts"] + 3600_000
    df = pd.merge_asof(df.sort_values("close_ts"), t.sort_values("avail_ts"),
                       left_on="close_ts", right_on="avail_ts", direction="backward")
    df["t4"] = df["t4"].fillna(0).astype(int)
    return df


def decide(mode, t4, t1, hours_since_switch, rule="both"):
    """Return 'OUT', 'IN' or None (no change)."""
    if hours_since_switch < config.SPOT_MIN_SWITCH_H:
        return None
    if rule == "both":
        down, up = (t4 == -1 and t1 == -1), (t4 == 1 and t1 == 1)
    elif rule == "4h":
        down, up = t4 == -1, t4 == 1
    else:  # "1h"
        down, up = t1 == -1, t1 == 1
    if mode == "IN" and down:
        return "OUT"
    if mode == "OUT" and up:
        return "IN"
    return None
