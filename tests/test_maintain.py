"""Planner-mode maintenance cycle: no automatic decisions, safety still runs.
Run: python3 tests/test_maintain.py"""
import os, sys, tempfile
os.environ.setdefault("OKXBOT_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import patch
import pandas as pd
import test_run_risk as R          # reuse fake exchange + cycle harness
from okxbot import config
from okxbot import bot as B

AUTO = ("AUTO_ENTRIES", "AUTO_TREND_EXITS", "AUTO_TRAIL", "AUTO_LIQ_NUDGE", "AUTO_SLEEVES")


def planner_mode():
    for k in AUTO:
        setattr(config, k, False)


def strong_signal_bot(failure=None, equity=1000, stop_algo=True):
    b = R.fake_bot(failure, equity)
    b.features.return_value = pd.DataFrame([{"ts": 0, "c": 150, "h": 150, "l": 100, "atr": 2,
                                             "signal": 1, "trend4h": -1, "rsi": 80, "strength": 9}])
    if stop_algo:
        b.x.algos.append({"instId": R.INST, "side": "sell", "sz": "2", "slTriggerPx": "90"})
    return b


def t_defaults_are_planner_mode():
    import importlib, okxbot.config as c
    src = open(c.__file__).read()
    for k in AUTO:
        assert f"{k} = False" in src, k


def t_no_entries_no_trend_exit_no_trail():
    planner_mode()
    b = strong_signal_bot()
    trail = patch.object(B, "update_stop", side_effect=AssertionError("trail called"))
    with trail:
        spot, guard, decision, _, _ = R.run_cycle(b, trend_exit=True)
    assert not b.x.orders and not b.x.closed, (b.x.orders, b.x.closed)
    assert "tickers" not in b.x.calls          # no universe / entry scan
    spot.run_alloc.assert_not_called()
    spot.enter_longs.assert_not_called()
    spot.manage_longs.assert_called_once()
    spot.recover_pending.assert_called_once()
    assert "protect" not in b.x.calls          # existing stop kept, not replaced
    assert b.st["positions"][R.INST]["stop"] == 90
    assert any("maintenance only" in e.get("msg", "") for e in decision["events"]), decision["events"]


def t_no_liq_nudge():
    planner_mode()
    b = strong_signal_bot()
    b.liq_nudge_open = lambda *a, **k: (_ for _ in ()).throw(AssertionError("nudge called"))
    with patch.object(config, "LIQMAP_NUDGE_OPEN", True):
        R.run_cycle(b)
    assert not b.x.orders


def t_fills_missing_stop():
    planner_mode()
    b = strong_signal_bot(stop_algo=False)
    R.run_cycle(b)
    assert "protect" in b.x.calls
    assert float(b.x.algos[0]["slTriggerPx"]) == 90


def t_breaker_still_flattens():
    planner_mode()
    b = strong_signal_bot(equity=800)        # -20% vs day start 1000 -> daily loss limit
    b.x.cancel_algos = lambda items: [{"sCode": "0"}]
    R.run_cycle(b)
    assert b.x.closed == [R.INST], b.x.closed
    assert not b.x.orders


def t_candles_failure_does_not_skip_coverage():
    planner_mode()
    b = strong_signal_bot(stop_algo=False)
    b.features.side_effect = B.OKXError("candles down")
    _, _, decision, _, _ = R.run_cycle(b)
    assert "protect" in b.x.calls
    assert any("stop coverage continues" in e for e in decision["errors"])


def t_run_once_is_maintain():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "run_once.py")).read()
    assert "from scripts.maintain import main" in src


def t_maintain_lock_and_alerts():
    import fcntl, io, contextlib
    from scripts import maintain
    lock = open(os.path.join(config.DATA_DIR, "run_once.lock"), "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = maintain.main([])
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
    assert code == 3 and "ALERTS:" in buf.getvalue()


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("t_"):
            f(); n += 1; print("PASS", k)
    print(f"{n} tests passed")
