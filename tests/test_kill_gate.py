"""Kill switch + independent risk_gate. Run: python3 tests/test_kill_gate.py"""
import os, sys, tempfile, time
os.environ.setdefault("OKXBOT_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from okxbot import config
from okxbot import risk as R
from okxbot import risk_gate as G

passed = failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed += 1
        print(f" FAIL {name}: {detail}")


def test_daily_loss_2pct():
    st = {"peak_equity": 1000, "day_start_equity": 1000}
    # -1.9% still ok
    can, flat, why = R.guardrail_status(dict(st), 981)
    check("daily -1.9% still open", can and not flat, why)
    # -2% locks day and flattens (existing daily-loss design)
    can, flat, why = R.guardrail_status(dict(st), 980)
    check("daily -2% locks", (not can) and flat and "daily loss" in why, why)
    check("DAILY_LOSS_LIMIT is 2%", abs(config.DAILY_LOSS_LIMIT - 0.02) < 1e-12)


def test_drawdown_40_still_hard():
    st = {"peak_equity": 1000, "day_start_equity": 1000}
    can, flat, why = R.guardrail_status(dict(st), 600)  # -40%
    check("40% DD hard halt", (not can) and flat and st.get("halted") is not True)  # state mutated
    st2 = {"peak_equity": 1000, "day_start_equity": 1000}
    can, flat, why = R.guardrail_status(st2, 599)
    check("over 40% DD halted", (not can) and flat and st2.get("halted") is True, why)


def test_consec_losses_pause():
    st = {}
    now = 1_700_000_000
    assert R.note_strategy_close(st, -1, intent="planner", now=now) is None
    assert R.note_strategy_close(st, -1, intent="planner", now=now) is None
    pause = R.note_strategy_close(st, -1, intent="planner", now=now)
    check("3rd loss arms pause", pause is not None and pause["kind"] == "consec_losses", pause)
    can, flat, why = R.guardrail_status(st, 1000, now=now + 10)
    check("soft pause: can_open false, no flatten", (not can) and (not flat), why)
    # win resets streak without clearing active pause
    R.note_strategy_close(st, 5, intent="planner", now=now + 20)
    check("pause still active after win", bool(R.active_entry_blocks(st, now + 20)))
    # after pause expires
    can, flat, why = R.guardrail_status(st, 1000, now=now + config.KILL_CONSEC_PAUSE_H * 3600 + 1)
    check("pause expires", can and not flat, why)
    # smoke intent ignored
    st2 = {}
    check("smoke ignored", R.note_strategy_close(st2, -10, intent="smoke", now=now) is None)


def test_entry_slippage_pause():
    st = {}
    now = 1_700_000_000
    check("within 0.5% ok", R.note_entry_slippage(st, 100, 100.4, "BTC-USDT-SWAP", now=now) is None)
    pause = R.note_entry_slippage(st, 100, 100.6, "BTC-USDT-SWAP", now=now)
    check("over 0.5% pauses", pause is not None and pause["slip"] > 0.005, pause)
    can, flat, _ = R.guardrail_status(st, 1000, now=now + 1)
    check("slip pause soft", (not can) and (not flat))


def fresh_snapshot(inst="BTC-USDT-SWAP", last=100.0, age_s=60, entry_risk=5.0):
    now_ms = int((time.time() - age_s) * 1000)
    return {
        "account": {"equity_usdt": 1000, "avail_usdt": 1000, "positions": []},
        "guardrails": {"can_open": True},
        "instruments": {
            inst: {
                "last": last,
                "spec": {"ctVal": "0.01", "lotSz": "1", "minSz": "1", "tickSz": "0.1"},
                "candles": {"1H": {"bars": [[now_ms, last, last, last, last, "1"]]}},
            }
        },
    }


def test_risk_gate_approve():
    snap = fresh_snapshot(age_s=60)
    action = {"action": "open", "instId": "BTC-USDT-SWAP", "market": "swap", "side": "long",
              "entry": 100.0, "stop": 99.0, "size": {"risk_usdt": 5.0}}
    g = G.check_open(action, snap, {})
    check("fresh+aligned APPROVE", g["decision"] == "APPROVE", g)


def test_risk_gate_stale():
    snap = fresh_snapshot(age_s=config.KILL_STALE_DATA_S + 50)
    action = {"action": "open", "instId": "BTC-USDT-SWAP", "market": "swap", "side": "long",
              "entry": 100.0, "stop": 99.0, "size": {"risk_usdt": 5.0}}
    g = G.check_open(action, snap, {})
    check("stale VETO", g["decision"] == "VETO" and any("stale" in r for r in g["reasons"]), g)


def test_risk_gate_no_chase():
    snap = fresh_snapshot(last=101.0, age_s=30)  # 1% away from plan 100
    action = {"action": "open", "instId": "BTC-USDT-SWAP", "market": "swap", "side": "long",
              "entry": 100.0, "stop": 99.0, "size": {"risk_usdt": 5.0}}
    g = G.check_open(action, snap, {})
    check("chase VETO", g["decision"] == "VETO" and any("chase" in r for r in g["reasons"]), g)


def test_correlation_group():
    check("BTC+ETH same group", G.correlation_group("BTC-USDT-SWAP") == G.correlation_group("ETH-USDT-SWAP")
          == "crypto_majors")
    check("QQQ+SPY us_index", G.correlation_group("QQQ-USDT-SWAP") == "us_index")
    check("CL+BZ crude", G.correlation_group("CL-USDT-SWAP") == G.correlation_group("BZ-USDT-SWAP") == "crude")
    check("XAU alone gold", G.correlation_group("XAU-USDT-SWAP") == "gold")
    check("unknown solo", G.correlation_group("FOO-USDT-SWAP").startswith("solo:"))


def test_correlation_veto():
    # Existing ETH long already uses full group budget (1x RISK_PER_TRADE * equity)
    equity = 1000.0
    budget = equity * config.RISK_PER_TRADE * config.CORRELATION_GROUP_MAX_RISK_UNITS
    state = {"positions": {
        "ETH-USDT-SWAP": {"side": 1, "entry": 100, "stop": 99, "contracts": 1,
                          "planned_risk_usdt": budget, "spec": {"ctVal": 1}}
    }}
    snap = fresh_snapshot(inst="BTC-USDT-SWAP", last=100.0, age_s=30)
    snap["account"]["positions"] = [{"instId": "ETH-USDT-SWAP", "side": "long", "size": 1, "avg_px": 100}]
    action = {"action": "open", "instId": "BTC-USDT-SWAP", "market": "swap", "side": "long",
              "entry": 100.0, "stop": 99.0, "size": {"risk_usdt": 1.0}}
    g = G.check_open(action, snap, state)
    check("corr veto same-dir majors", g["decision"] == "VETO" and any("correlation" in r for r in g["reasons"]), g)
    # Opposite side OK
    action["side"] = "short"
    action["stop"] = 101.0
    g2 = G.check_open(action, snap, state)
    check("opp side not vetoed by long bucket", g2["decision"] == "APPROVE", g2)


def test_kill_status_lines():
    st = {"kill": {"consec_losses": 2}}
    lines = R.kill_status_lines(st)
    check("streak visible", any("consecutive losses streak: 2" in x for x in lines), lines)


if __name__ == "__main__":
    print("=== test_kill_gate ===")
    for fn in (test_daily_loss_2pct, test_drawdown_40_still_hard, test_consec_losses_pause,
               test_entry_slippage_pause, test_risk_gate_approve, test_risk_gate_stale,
               test_risk_gate_no_chase, test_correlation_group, test_correlation_veto,
               test_kill_status_lines):
        print(f"-- {fn.__name__}")
        fn()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
