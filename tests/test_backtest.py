"""Offline backtest window regressions. Run: .venv/bin/python tests/test_backtest.py"""
import math
import os
import sys
from contextlib import ExitStack
from unittest.mock import patch

import pandas as pd

import tempfile
os.environ.setdefault("OKXBOT_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtests import backtest as B

HOUR = 3600_000


def frame(hours, closes, signals=None):
    return pd.DataFrame({"ts": [h * HOUR for h in hours], "o": 100.0,
                         "h": [max(101, c) for c in closes], "l": 99.0,
                         "c": closes, "atr": 1.0, "strength": 1.0,
                         "signal": signals or [1] + [0] * (len(hours) - 1)})


def run_frames(frames, **kwargs):
    with ExitStack() as stack:
        stack.enter_context(patch.object(B, "load", side_effect=lambda inst, n: frames[inst].copy()))
        stack.enter_context(patch.object(B, "build_features", side_effect=lambda df: df))
        stack.enter_context(patch.object(B, "OKX", side_effect=AssertionError("unexpected network client")))
        stack.enter_context(patch.object(B, "initial_stop_tp", return_value=(1.0, 10000.0)))
        stack.enter_context(patch.object(B, "update_stop", return_value=1.0))
        stack.enter_context(patch.object(B, "should_exit_on_trend", return_value=False))
        # One unit per position makes fees and slippage independently checkable.
        stack.enter_context(patch.object(B, "size_position", side_effect=lambda eq, entry, stop: (entry, 1, entry)))
        return B.run(list(frames), 10, **kwargs)


def assert_final_cash(result, trades, curve):
    cash = result["start_eq"] + sum(t["pnl"] for t in trades)
    assert math.isclose(curve[-1][1], cash, abs_tol=1e-10), (curve, cash)
    # Summary equity is rounded to cents; the curve retains full precision.
    assert round(curve[-1][1], 2) == result["final_eq"]


def t_bounded_exit_uses_each_instruments_last_eligible_bar():
    frames = {"A": frame([0, 1, 2, 3, 4], [100, 100, 102, 800, 900]),
              "B": frame([0, 1, 3, 4], [100, 101, 700, 800])}
    result, trades, curve = run_frames(frames, t_from=0, t_to=3 * HOUR)
    assert len(trades) == 2, trades
    expected = {"A": (2 * HOUR, 102), "B": (HOUR, 101)}
    for trade in trades:
        ts, price = expected[trade["inst"]]
        assert trade["why"] == "end"
        assert trade["exit_ts"] == ts < 3 * HOUR
        assert math.isclose(trade["exit"], price * (1 - B.SLIP))
    assert curve[-1][0] == 2 * HOUR
    assert_final_cash(result, trades, curve)


def t_from_excludes_earlier_signals_and_bars():
    frames = {"A": frame([0, 1, 2, 3, 4], [100] * 5, [1, 0, 1, 0, 0])}
    for t_to in (4 * HOUR, None):
        result, trades, curve = run_frames(frames, t_from=2 * HOUR, t_to=t_to)
        assert len(trades) == 1 and trades[0]["entry_ts"] == 3 * HOUR, trades
        assert all(ts >= 2 * HOUR for ts, _ in curve)
        assert_final_cash(result, trades, curve)


def t_final_cash_includes_exit_costs_in_drawdown():
    result, trades, curve = run_frames({"A": frame([0, 1, 2], [100] * 3)}, t_to=2 * HOUR)
    assert len(trades) == 1 and len(curve) == 3
    exit_cost = 100 * B.SLIP + trades[0]["exit"] * B.FEE
    assert math.isclose(curve[-2][1] - curve[-1][1], exit_cost)
    assert_final_cash(result, trades, curve)
    expected_dd = round((1 - curve[-1][1] / curve[0][1]) * 100, 2)
    pre_exit_dd = round((1 - curve[-2][1] / curve[0][1]) * 100, 2)
    assert result["max_dd_pct"] == expected_dd > pre_exit_dd


def t_unbounded_preserves_existing_final_closes():
    frames = {"A": frame([0, 1, 2], [100, 100, 103]),
              "B": frame([0, 1, 2, 3], [100, 100, 103, 105])}
    result, trades, curve = run_frames(frames)
    assert len(trades) == 2 and len(curve) == 5
    for trade in trades:
        price = frames[trade["inst"]]["c"].iloc[-1]
        assert trade["exit_ts"] == 3 * HOUR and trade["why"] == "end"
        assert math.isclose(trade["exit"], price * (1 - B.SLIP))
    assert curve[-1][0] == curve[-2][0] == 3 * HOUR
    assert_final_cash(result, trades, curve)


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("t_"):
            f(); n += 1; print("PASS", k)
    print(f"{n} tests passed")
