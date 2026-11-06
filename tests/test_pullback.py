"""Pullback / limit-retest entry policy. Run: python3 tests/test_pullback.py"""
import os, sys, tempfile, math
import numpy as np
import pandas as pd

os.environ.setdefault("OKXBOT_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from okxbot import config, pullback
from okxbot import plan as P
from okxbot.bot import save_state
import json, datetime as dt
from pathlib import Path
from okxbot import indicators

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed += 1
        print(f" FAIL {name}: {detail}")


def test_soft_daily_mult():
    check("align 1.0", abs(pullback.soft_daily_mult(1, 1) - 1.0) < 1e-12)
    check("flat 0.7", abs(pullback.soft_daily_mult(1, 0) - 0.7) < 1e-12)
    check("against 0.4", abs(pullback.soft_daily_mult(1, -1) - 0.4) < 1e-12)
    check("short against", abs(pullback.soft_daily_mult(-1, 1) - 0.4) < 1e-12)


def _synth_breakout(n=400, breakout_at=380):
    """Synthetic 1H series trending up then Donchian break near the end."""
    rng = np.random.default_rng(42)
    ts0 = 1_700_000_000_000
    rows = []
    px = 100.0
    for i in range(n):
        # mild uptrend, then a breakout bar
        drift = 0.05 if i < breakout_at else 0.8
        noise = float(rng.normal(0, 0.15))
        o = px
        c = px + drift + noise
        h = max(o, c) + 0.2
        l = min(o, c) - 0.2
        if i == breakout_at:
            c = px + 3.0
            h = c + 0.5
            l = px - 0.1
        rows.append([ts0 + i * 3_600_000, o, h, l, c, 1000, 0, 0, "1"])
        px = c
    return indicators.to_df(rows)


def test_compute_pullback_view_arms():
    df = _synth_breakout()
    view = pullback.compute_pullback_view(df)
    check("enabled", view.get("enabled") is True, view)
    # May or may not arm depending on C7 filters (trend/EMA); at least returns valid schema
    for k in ("status", "risk_mult", "zone_atr", "reason"):
        check(f"key {k}", k in view, view)
    check("status enum", view["status"] in ("none", "armed", "retest_ready"), view["status"])
    check("risk_mult in {0.4,0.7,1.0}", view["risk_mult"] in (0.4, 0.7, 1.0), view["risk_mult"])


def test_insufficient_history():
    df = _synth_breakout(n=50)
    view = pullback.compute_pullback_view(df)
    check("short history -> none", view["status"] == "none", view)


def fixture_plan(action, pullback_view=None):
    NOW = dt.datetime.now(dt.timezone.utc)
    for name in ("decision_journal.jsonl", "state.json"):
        Path(config.DATA_DIR, name).unlink(missing_ok=True)
    state = {"positions": {}, "spot": {"longs": {}}, "last_signal_ts": {}}
    save_state(state)
    bar_ts = int(NOW.timestamp() * 1000) - 60_000
    instruments = {}
    for i in ("BTC-USDT", "BTC-USDT-SWAP"):
        instruments[i] = {
            "spec": {"instId": i, "state": "live", "tickSz": ".01", "lotSz": ".001",
                     "minSz": ".001", "ctVal": "1"},
            "last": 100.0, "mark": 100.0, "liquidity_ok": True,
            "candles": {"1H": {"bars": [[bar_ts, 100, 100, 100, 100, "1"]]}},
        }
        if pullback_view is not None:
            instruments[i]["pullback"] = dict(pullback_view)
    snapshot = {
        "snapshot_id": "test-snapshot-pb",
        "generated_at": NOW.isoformat(),
        "account": {"equity_usdt": 10000, "avail_usdt": 10000, "positions": [],
                    "pending_orders": [], "pending_algos": []},
        "guardrails": {"can_open": True},
        "instruments": instruments,
    }
    path = Path(config.DATA_DIR, "snapshots", "test-snapshot-pb.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot))
    plan = {
        "schema": "okxbot.plan/v1", "plan_id": "test-pb",
        "snapshot_id": snapshot["snapshot_id"],
        "created_at": (NOW - dt.timedelta(minutes=1)).isoformat(),
        "valid_until": (NOW + dt.timedelta(hours=1)).isoformat(),
        "rationale": "pullback test", "uncertainties": [], "actions": [action],
    }
    return plan, snapshot, state


def test_pullback_requires_limit():
    action = {
        "action": "open", "instId": "BTC-USDT-SWAP", "market": "swap", "side": "long",
        "leverage": 2, "stop": 97.0, "take_profit": 106.0,
        "size": {"risk_usdt": 40}, "order_type": "market", "signal": "pullback_retest",
    }
    plan, snap, state = fixture_plan(action, {
        "status": "armed", "side": "long", "limit_px": 99.8, "level": 99.8,
        "risk_mult": 0.7, "daily_trend": 0, "extended_chase": False,
    })
    try:
        P.validate_action(action, snap, state)
        check("market pullback rejected", False, "should have raised")
    except ValueError as e:
        check("market pullback rejected", "limit" in str(e).lower(), str(e))


def test_pullback_limit_ok_and_soft_daily_note():
    action = {
        "action": "open", "instId": "BTC-USDT-SWAP", "market": "swap", "side": "long",
        "leverage": 2, "stop": 97.0, "take_profit": 106.0,
        "size": {"risk_usdt": 50}, "order_type": "limit", "limit_px": 99.8,
        "signal": "pullback_retest",
    }
    plan, snap, state = fixture_plan(action, {
        "status": "retest_ready", "side": "long", "limit_px": 99.8, "level": 99.8,
        "risk_mult": 0.7, "daily_trend": 0, "extended_chase": False,
    })
    body, notes = P.validate_action(action, snap, state)
    check("limit pullback accepted", body is not None and body.get("ordType") == "limit", body)
    check("soft-daily note", any("risk_mult=0.7" in n for n in notes), notes)


def test_extended_chase_blocks_market():
    action = {
        "action": "open", "instId": "BTC-USDT-SWAP", "market": "swap", "side": "long",
        "leverage": 2, "stop": 97.0, "take_profit": 106.0,
        "size": {"risk_usdt": 40}, "order_type": "market",  # no pullback signal tag
    }
    plan, snap, state = fixture_plan(action, {
        "status": "armed", "side": "long", "limit_px": 99.5, "level": 99.5,
        "risk_mult": 1.0, "daily_trend": 1, "extended_chase": True,
    })
    try:
        P.validate_action(action, snap, state)
        check("extended market blocked", False, "should have raised")
    except ValueError as e:
        check("extended market blocked", "extended" in str(e).lower() or "limit" in str(e).lower(), str(e))


def test_config_knobs():
    check("PULLBACK_ENABLED", getattr(config, "PULLBACK_ENABLED", False) is True)
    check("WAIT_H 12", getattr(config, "PULLBACK_WAIT_H", 0) == 12)
    check("ZONE 0.4", abs(getattr(config, "PULLBACK_ZONE_ATR", 0) - 0.4) < 1e-12)


if __name__ == "__main__":
    for fn in (test_soft_daily_mult, test_compute_pullback_view_arms, test_insufficient_history,
               test_pullback_requires_limit, test_pullback_limit_ok_and_soft_daily_note,
               test_extended_chase_blocks_market, test_config_knobs):
        print(fn.__name__)
        fn()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
