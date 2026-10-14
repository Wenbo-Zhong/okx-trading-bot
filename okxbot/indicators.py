import numpy as np
import pandas as pd


def to_df(rows):
    df = pd.DataFrame([r[:9] for r in rows],
                      columns=["ts", "o", "h", "l", "c", "vol", "volCcy", "volQuote", "confirm"])
    for k in ["o", "h", "l", "c", "vol", "volCcy", "volQuote"]:
        df[k] = df[k].astype(float)
    df["ts"] = df["ts"].astype("int64")
    df["confirm"] = df["confirm"].astype(str)
    return df.reset_index(drop=True)


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df, n=14):
    pc = df["c"].shift()
    tr = pd.concat([df["h"] - df["l"], (df["h"] - pc).abs(), (df["l"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def add_indicators(df, donchian_n=20):
    df = df.copy()
    df["ema20"] = ema(df["c"], 20)
    df["ema50"] = ema(df["c"], 50)
    df["ema200"] = ema(df["c"], 200)
    macd = ema(df["c"], 12) - ema(df["c"], 26)
    df["macd_hist"] = macd - ema(macd, 9)
    df["rsi"] = rsi(df["c"])
    df["atr"] = atr(df)
    df["hh20"] = df["h"].rolling(donchian_n).max().shift(1)   # prior N-bar high
    df["ll20"] = df["l"].rolling(donchian_n).min().shift(1)
    return df
