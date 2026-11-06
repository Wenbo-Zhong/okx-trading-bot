"""Pullback / limit-retest entry helpers (DEMO planner policy, 2026-11-06).

When C7 (or a Donchian breakout) arms, do not market-chase. Arm a limit at the
Donchian break level or 1H EMA20 (± zone*ATR) for up to PULLBACK_WAIT_H hours.
Daily trend is a size scaler only (align / flat / against) — never a hard veto.

Pure functions; snapshot attaches the view, plan validation enforces limit + size.
"""
from __future__ import annotations

import math
from okxbot import config
from okxbot import strategy
from okxbot.indicators import ema


def soft_daily_mult(side: int, daily_trend: int) -> float:
    """Size scaler: same 1.0×, flat 0.7×, against 0.4×."""
    if not side:
        return 1.0
    align = float(getattr(config, "PULLBACK_DAILY_ALIGN", 1.0))
    flat = float(getattr(config, "PULLBACK_DAILY_FLAT", 0.7))
    against = float(getattr(config, "PULLBACK_DAILY_AGAINST", 0.4))
    if int(daily_trend) == int(side):
        return align
    if int(daily_trend) == 0:
        return flat
    return against


def daily_trend_from_1h(df1h):
    """No-lookahead daily trend from closed 1H bars (same idea as bt_entry_research).

    Returns int Series aligned to df1h rows: +1 / -1 / 0. Insufficient history → 0.
    """
    import numpy as np
    import pandas as pd

    d = df1h.copy()
    d["dt"] = pd.to_datetime(d["ts"], unit="ms", utc=True)
    g = d.set_index("dt").resample("1D", label="left", closed="left")
    day = pd.DataFrame({"o": g["o"].first(), "h": g["h"].max(), "l": g["l"].min(),
                        "c": g["c"].last(), "n": g["c"].count()}).dropna()
    day = day[day["n"] >= 6]
    if len(day) < 25:
        return pd.Series(0, index=df1h.index, dtype=int)
    day["ema20"] = ema(day["c"], 20)
    day["ema50"] = ema(day["c"], 50)
    trend = np.where((day["c"] > day["ema50"]) & (day["ema20"] > day["ema50"]), 1,
                     np.where((day["c"] < day["ema50"]) & (day["ema20"] < day["ema50"]), -1, 0))
    t = pd.DataFrame({"avail_ts": day.index.asi8 // 10**6 + 24 * 3600_000, "trend_d": trend})
    out = df1h.copy()
    out["close_ts"] = out["ts"] + 3600_000
    out = pd.merge_asof(out.sort_values("close_ts"), t.sort_values("avail_ts"),
                        left_on="close_ts", right_on="avail_ts", direction="backward")
    return out["trend_d"].fillna(0).astype(int).reset_index(drop=True)


def _finite(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _arm_level(row, side: int):
    """Prefer Donchian break level; fall back to EMA20."""
    don = _finite(row["hh20"] if side == 1 else row["ll20"])
    e20 = _finite(row.get("ema20"))
    if don is not None:
        return don, "donchian"
    if e20 is not None:
        return e20, "ema20"
    return None, None


def _in_retest_zone(row, side: int, level: float, atr_v: float, zone_atr: float) -> bool:
    z = zone_atr * atr_v
    lo, hi, c = float(row["l"]), float(row["h"]), float(row["c"])
    e20 = _finite(row.get("ema20"))
    hit = (side == 1 and lo <= level + z and c >= level - z) or \
          (side == -1 and hi >= level - z and c <= level + z)
    hit_ema = False
    if e20 is not None:
        ll = _finite(row.get("ll20"))
        hh = _finite(row.get("hh20"))
        hit_ema = (side == 1 and lo <= e20 + z and c > e20 - z and (ll is None or c > ll)) or \
                  (side == -1 and hi >= e20 - z and c < e20 + z and (hh is None or c < hh))
    return bool(hit or hit_ema)


def _empty(enabled, zone_atr, reason):
    return {
        "enabled": enabled, "status": "none", "side": None, "level": None,
        "level_source": None, "atr": None, "zone_atr": zone_atr, "zone": None,
        "limit_px": None, "risk_mult": 1.0, "daily_trend": 0,
        "armed_bar_ts": None, "expires_at_ms": None, "hours_left": None,
        "extended_chase": False, "stop": None, "tp": None, "reason": reason,
    }


def compute_pullback_view(frame_1h, now_ms=None):
    """Build the per-instrument pullback view for the snapshot / planner.

    status:
      - none: no active arm in the lookback window
      - armed: C7/breakout armed a limit level; waiting for retest
      - retest_ready: last closed bar is inside the retest zone (limit may fill)
    """
    wait_h = int(getattr(config, "PULLBACK_WAIT_H", 12))
    zone_atr = float(getattr(config, "PULLBACK_ZONE_ATR", 0.4))
    enabled = bool(getattr(config, "PULLBACK_ENABLED", True))
    if not enabled:
        return _empty(False, zone_atr, "pullback disabled")
    if frame_1h is None or len(frame_1h) < 300:
        return _empty(True, zone_atr, "insufficient closed 1H history for pullback view")

    feat = strategy.build_features(frame_1h).reset_index(drop=True)
    feat["trend_d"] = daily_trend_from_1h(feat).values
    wait_bars = wait_h
    n = len(feat)
    # Walk the whole warm window so an arm raised earlier in the last wait_h hours survives.
    pending = None  # dict
    for i in range(n):
        r = feat.iloc[i]
        if pending is not None:
            if i > pending["arm_i"] + wait_bars or int(r["trend4h"]) != pending["side"]:
                pending = None
        if pending is None and int(r["signal"]) != 0:
            side = int(r["signal"])
            level, source = _arm_level(r, side)
            atr_v = _finite(r.get("atr"))
            if level is None or atr_v is None or atr_v <= 0:
                continue
            pending = {
                "side": side, "level": level, "source": source,
                "arm_i": i, "atr": atr_v,
            }

    if pending is None:
        return _empty(True, zone_atr, "no C7/breakout arm in window")

    side = pending["side"]
    level = pending["level"]
    atr_v = pending["atr"]
    arm_i = pending["arm_i"]
    last = feat.iloc[-1]
    hours_left = pending["arm_i"] + wait_bars - (n - 1)
    if hours_left < 0:
        return _empty(True, zone_atr, "arm expired without retest")
    if int(last["trend4h"]) != side:
        # Trend flip cancels unless we are already in the retest zone this bar.
        if not _in_retest_zone(last, side, level, atr_v, zone_atr):
            return _empty(True, zone_atr, "1H trend flipped against arm")

    daily = int(last["trend_d"])
    mult = soft_daily_mult(side, daily)
    stop, tp = strategy.initial_stop_tp(side, level, atr_v)
    last_px = _finite(last.get("c"))
    chase_lim = float(getattr(config, "KILL_ENTRY_SLIP_PCT", 0.005))
    retest = _in_retest_zone(last, side, level, atr_v, zone_atr)
    extended = False
    if last_px and level:
        if side == 1 and last_px > level * (1 + chase_lim):
            extended = not retest
        elif side == -1 and last_px < level * (1 - chase_lim):
            extended = not retest

    status = "retest_ready" if retest else "armed"
    reason = ("retest zone live — prefer limit at level" if retest
              else "C7/breakout armed — wait for limit retest, do not market-chase")
    if extended:
        reason += "; price extended > chase limit from level (limit only / skip market)"

    expires_at_ms = int(feat.iloc[arm_i]["ts"]) + (wait_bars + 1) * 3_600_000
    return {
        "enabled": True,
        "status": status,
        "side": "long" if side == 1 else "short",
        "level": round(level, 10),
        "level_source": pending["source"],
        "atr": round(atr_v, 10),
        "zone_atr": zone_atr,
        "zone": round(zone_atr * atr_v, 10),
        "limit_px": round(level, 10),
        "risk_mult": mult,
        "daily_trend": daily,
        "armed_bar_ts": int(feat.iloc[arm_i]["ts"]),
        "expires_at_ms": expires_at_ms,
        "hours_left": int(hours_left),
        "extended_chase": extended,
        "stop": None if stop is None else round(float(stop), 10),
        "tp": None if tp is None else round(float(tp), 10),
        "reason": reason,
    }


def risk_mult_for_action(action, instrument_pullback=None) -> float:
    """Resolve soft-daily / pullback size scaler for an open action."""
    for src in (action, instrument_pullback or {}):
        if src and src.get("risk_mult") is not None:
            try:
                m = float(src["risk_mult"])
                if 0 < m <= 1.0 + 1e-12:
                    return m
            except (TypeError, ValueError):
                pass
    pb = instrument_pullback or {}
    side = action.get("side")
    side_i = 1 if side == "long" else -1 if side == "short" else 0
    if pb.get("daily_trend") is not None and side_i:
        return soft_daily_mult(side_i, int(pb["daily_trend"]))
    return 1.0
