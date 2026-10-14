"""Round-trip reports aggregate partial executions without closing live remainders."""
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("OKXBOT_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from okxbot import journal as J, config

OPEN = {"ev": "open", "time_ms": 1000, "trade_id": "entry", "instId": "TEST-USDT",
        "market": "spot", "side": "long", "entry": 10, "size": 100, "init_stop": 9,
        "planned_risk_usdt": 100}
PARTIAL = {"ev": "close", "time_ms": 2000, "trade_id": "entry", "instId": "TEST-USDT",
           "execution_id": "sell1:40", "size": 40, "remaining": 60, "exit": 12,
           "realized_pnl": 78, "fee": -2}
FINAL = {"ev": "close", "time_ms": 3000, "trade_id": "entry", "instId": "TEST-USDT",
         "execution_id": "sell2:60", "size": 60, "remaining": 0, "exit": 13,
         "realized_pnl": 177, "fee": -3}


def build(events, remaining=60):
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        with open(os.path.join(tmp, "state.json"), "w") as file:
            json.dump({"spot": {"longs": {"TEST-USDT": {"qty": remaining}}}}, file)
        stack.enter_context(patch.object(config, "DATA_DIR", tmp))
        stack.enter_context(patch.object(J, "CSV_F", os.path.join(tmp, "journal.csv")))
        stack.enter_context(patch.object(J, "MD_F", os.path.join(tmp, "journal.md")))
        stack.enter_context(patch.object(J.J, "load", return_value=events))
        stack.enter_context(patch.object(J, "OKX", return_value=SimpleNamespace(
            has_keys=False, _req=Mock(return_value=[{"last": "13"}]))))
        stack.enter_context(patch.object(J, "mfe_mae", return_value=(None, None)))
        stack.enter_context(patch.object(J, "high_impact_runs", return_value=[]))
        stack.enter_context(patch.object(J, "causes", return_value=[]))
        result = J.build()
        report = open(J.MD_F).read()
    return result[0], report


def t_partial_close_stays_open_and_marks_only_remaining_quantity():
    row, report = build([OPEN, PARTIAL])
    assert row["status"] == "open" and row["close_time_bj"] == ""
    assert row["realized_pnl_usdt"] == 78 and row["unrealized_pnl_usdt"] == 180
    assert row["exit_reason"] == "open"
    assert "已实现盈亏合计：+78.00" in report
    assert "| 现货 | 0/1 | +78.00 |" in report


def t_full_close_sums_pnl_fees_and_weights_exit_price():
    row, _ = build([OPEN, FINAL, PARTIAL], remaining=0)
    assert row["status"] == "closed" and row["close_time_bj"]
    assert row["realized_pnl_usdt"] == 255 and row["fee_usdt"] == -5
    assert row["price_pnl_usdt"] == 260
    assert abs(row["exit"] - 12.6) < 1e-9


def t_duplicate_execution_is_not_counted_twice():
    row, _ = build([OPEN, OPEN, PARTIAL, PARTIAL, FINAL], remaining=0)
    assert row["realized_pnl_usdt"] == 255 and row["fee_usdt"] == -5


def t_legacy_single_close_remains_supported():
    legacy = dict(FINAL)
    legacy.pop("remaining")
    legacy.pop("size")
    row, _ = build([OPEN, legacy], remaining=0)
    assert row["status"] == "closed" and row["realized_pnl_usdt"] == 177
    assert row["exit"] == 13


if __name__ == "__main__":
    with patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")):
        tests = [fn for name, fn in list(globals().items()) if name.startswith("t_")]
        for test in tests:
            test()
            print("PASS", test.__name__)
        print(f"{len(tests)} tests passed")
