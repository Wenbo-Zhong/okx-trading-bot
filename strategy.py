"""Technical strategy: 4h trend filter + 1h Donchian breakout with momentum confirmation.
ATR-based stop, fixed-R take profit, breakeven + chandelier trailing stop.
Same code is used by the live bot and the backtester."""
import numpy as np
import pandas as pd
import config
from indicators import add_indicators, ema


def resample_4h(df1h):
    d = df1h.copy()
    d["dt"] = pd.to_datetime(d["ts"], unit="ms", utc=True).dt.as_unit("ms")
    g = d.set_index("dt").resample("4h", label="left", closed="left")
    out = pd.DataFrame({"o": g["o"].first(), "h": g["h"].max(), "l": g["l"].min(),
                        "c": g["c"].last(), "n": g["c"].count()}).dropna()
    out["ema20"] = ema(out["c"], 20)
    out["ema50"] = ema(out["c"], 50)
    return out


def build_features(df1h):
    """df1h: closed 1h candles, oldest first. Returns 1h frame with 4h trend merged (no look-ahead)."""
    df = add_indicators(df1h, config.DONCHIAN_N)
    h4 = resample_4h(df1h)
    # a 4h bar is only usable after it has fully closed: available from its start + 4h
    h4 = h4[h4["n"] == 4]
    h4["avail_ts"] = h4.index.as_unit("ms").asi8 + 4 * 3600_000   # ms epoch
    trend = np.where((h4["c"] > h4["ema50"]) & (h4["ema20"] > h4["ema50"]), 1,
                     np.where((h4["c"] < h4["ema50"]) & (h4["ema20"] < h4["ema50"]), -1, 0))
    t = pd.DataFrame({"avail_ts": h4["avail_ts"].values, "trend4h": trend})
    # 1h bar with open ts closes at ts+1h; 4h info available at avail_ts <= ts+1h
    df["close_ts"] = df["ts"] + 3600_000
    df = pd.merge_asof(df.sort_values("close_ts"), t.sort_values("avail_ts"),
                       left_on="close_ts", right_on="avail_ts", direction="backward")
    df["trend4h"] = df["trend4h"].fillna(0).astype(int)
    if getattr(config, "TREND_TF", "4h") == "1h":
        # 1h trend: close vs EMA200 and EMA50 vs EMA200 (column name kept as "trend4h" = active trend filter)
        df["trend4h"] = np.where((df["c"] > df["ema200"]) & (df["ema50"] > df["ema200"]), 1,
                                 np.where((df["c"] < df["ema200"]) & (df["ema50"] < df["ema200"]), -1, 0))
    use_macd = getattr(config, "USE_MACD", True)
    rsi_cap = getattr(config, "RSI_CAP", 75)
    align = getattr(config, "REQUIRE_EMA_ALIGN", True)
    long_sig = ((df["trend4h"] == 1) & (df["c"] > df["hh20"])
                & ((df["ema20"] > df["ema50"]) | (not align))
                & ((df["macd_hist"] > 0) | (not use_macd)) & (df["rsi"] < rsi_cap))
    short_sig = ((df["trend4h"] == -1) & (df["c"] < df["ll20"])
                 & ((df["ema20"] < df["ema50"]) | (not align))
                 & ((df["macd_hist"] < 0) | (not use_macd)) & (df["rsi"] > 100 - rsi_cap))
    df["signal"] = np.where(long_sig, 1, np.where(short_sig, -1, 0))
    df["strength"] = ((df["c"] - df["ema50"]) / df["atr"]).abs()
    return df


def initial_stop_tp(side, entry, atr_val):
    dist = config.ATR_STOP_MULT * atr_val
    dist = max(dist, entry * config.MIN_STOP_PCT)
    if dist > entry * config.MAX_STOP_PCT:
        return None, None
    if side == 1:
        return entry - dist, entry + config.TP_R_MULT * dist
    return entry + dist, entry - config.TP_R_MULT * dist


def update_stop(side, entry, init_stop, cur_stop, high_since, low_since, atr_val):
    """Return new stop (only ever tightens)."""
    r = abs(entry - init_stop)
    new = cur_stop
    if side == 1:
        if high_since - entry >= config.BREAKEVEN_AT_R * r:
            new = max(new, entry * 1.001)     # breakeven + fees
        if high_since - entry >= config.TRAIL_START_R * r:
            new = max(new, high_since - config.TRAIL_ATR_MULT * atr_val)
    else:
        if entry - low_since >= config.BREAKEVEN_AT_R * r:
            new = min(new, entry * 0.999)
        if entry - low_since >= config.TRAIL_START_R * r:
            new = min(new, low_since + config.TRAIL_ATR_MULT * atr_val)
    return new


def should_exit_on_trend(side, row):
    """Discretionary exit: 4h trend turned against the position."""
    return (side == 1 and row["trend4h"] == -1) or (side == -1 and row["trend4h"] == 1)
