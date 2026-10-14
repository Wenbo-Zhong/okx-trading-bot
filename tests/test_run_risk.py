"""Risk management survives universe outages. Run: .venv/bin/python tests/test_run_risk.py"""
import json
import os
import sys
from contextlib import ExitStack
from unittest.mock import Mock, patch

import pandas as pd

import tempfile
os.environ.setdefault("OKXBOT_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from okxbot import bot as B
from okxbot import config

INST = "TEST-USDT-SWAP"
SPEC = {"instId": INST, "state": "live", "instCategory": "1", "settleCcy": "USDT",
        "ctType": "linear", "tickSz": "0.01", "ctVal": "1", "lotSz": "1", "minSz": "1"}


class FakeX:
    def __init__(self, failure, equity=1000):
        self.failure, self.equity = failure, equity
        self.calls, self.orders, self.algos, self.closed = [], [], [], []

    def account_config(self):
        self.calls.append("account")
        if self.failure == "account":
            raise B.OKXError("account unavailable")
        return [{"posMode": "net_mode", "acctLv": "3"}]

    def balance(self):
        return [{"totalEq": str(self.equity), "details": [{"ccy": "USDT", "availEq": "1000"}]}]

    def positions(self):
        return [{"instId": INST, "pos": "2", "posSide": "net", "avgPx": "100",
                 "last": "105", "markPx": "105", "upl": "10"}]

    def tickers(self, inst_type):
        self.calls.append("tickers")
        if self.failure == "tickers":
            raise B.OKXError("tickers unavailable")
        return [{"instId": INST, "last": "105", "volCcy24h": "1000000"}]

    def instruments(self, inst_type, demo=False):
        self.calls.append("instruments")
        if self.failure == "instruments" or (self.failure == "demo_instruments" and demo):
            raise B.OKXError("instruments unavailable")
        return [SPEC.copy()]

    def _req(self, method, path, params, **kwargs):
        assert path == "/api/v5/public/instruments", path
        assert params["instId"] == INST
        assert kwargs["demo_public"] is True
        return [SPEC.copy()]

    def pending_algos(self, inst_id):
        return [dict(a, algoId=f"stop{i}") for i, a in enumerate(self.algos)]

    def place_algo(self, body):
        self.calls.append("protect")
        self.algos.append(body)
        return [{"algoId": "stop"}]

    def close_position(self, inst_id, pos_side):
        self.calls.append("close")
        self.closed.append(inst_id)

    def place_order(self, body):
        self.orders.append(body)
        raise AssertionError("unexpected new order")


def fake_bot(failure, equity=1000):
    b = B.Bot.__new__(B.Bot)
    b.x, b.live, b.mode = FakeX(failure, equity), True, "demo"
    b.swaps_blocked, b.block_longs, b.size_mult = False, False, 1.0
    b.usdt_avail, b.funding = 1000, {}
    b.st = {"positions": {INST: {"side": 1, "entry": 100, "init_stop": 90,
                                "stop": 90, "entry_ts": 0}},
            "last_signal_ts": {}, "day": B.now_local().date().isoformat(),
            "day_start_equity": 1000, "peak_equity": 1000}
    b.log = {"events": [], "errors": []}
    b.features = Mock(return_value=pd.DataFrame([{"ts": 0, "c": 105, "h": 105, "l": 100,
                                                "atr": 2, "signal": 0, "trend4h": 1, "rsi": 50}]))
    return b


def run_cycle(b, trend_exit=False, configure_spot=None):
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        for name, filename in [("STATE_F", "state.json"), ("DECISIONS_F", "decisions.jsonl"),
                               ("EQUITY_F", "equity.csv"), ("HALT_F", "HALTED")]:
            stack.enter_context(patch.object(B, name, os.path.join(tmp, filename)))
        stack.enter_context(patch.object(config, "DEMO_USE_FULL_EQUITY", True))
        stack.enter_context(patch.object(config, "LIQMAP_NUDGE_OPEN", False))
        stack.enter_context(patch.object(config, "TRADFI_ENABLED", False))
        stack.enter_context(patch.object(B, "log_trade"))
        stack.enter_context(patch.object(B.J, "record"))
        stack.enter_context(patch.object(B, "should_exit_on_trend", return_value=trend_exit))
        stack.enter_context(patch.object(B.news, "sentiment", return_value={
            "market": 0, "coins": {}, "n": 0, "errors": [], "high_impact": []}))
        stack.enter_context(patch.object(B.macro_events, "risk_window", return_value=(None, None)))
        stack.enter_context(patch.object(B.funding, "all_rates", return_value={}))
        spot = stack.enter_context(patch.object(B, "SpotManager")).return_value
        if configure_spot:
            configure_spot(spot)
        guardrail = B.guardrail_status

        def check_guardrails(state, equity):
            b.x.calls.append("guardrails")
            return guardrail(state, equity)

        guard = stack.enter_context(patch.object(B, "guardrail_status", side_effect=check_guardrails))
        b.run()
        decision = json.loads(open(B.DECISIONS_F).read())
        equity_rows = open(B.EQUITY_F).read().splitlines() if os.path.exists(B.EQUITY_F) else []
        halted = os.path.exists(B.HALT_F)
    return spot, guard, decision, equity_rows, halted


def assert_outage(b, result, n_pos):
    spot, guard, decision, equity_rows, _ = result
    guard.assert_called_once()
    assert b.log["equity"] == b.x.equity
    assert b.st["start_equity"] == b.x.equity
    assert any(e.startswith("universe fetch failed:") for e in decision["errors"])
    assert not any(e.startswith("manage ") for e in decision["errors"]), decision["errors"]
    spot.run_alloc.assert_called_once_with(can_rebuy=False)
    spot.manage_longs.assert_called_once()
    spot.enter_longs.assert_not_called()
    assert not b.x.orders
    assert len(equity_rows) == 2
    row = equity_rows[1].split(",")
    assert float(row[1]) == b.x.equity and row[2:] == [str(n_pos), "demo"]
    assert b.x.calls.index("guardrails") < b.x.calls.index("tickers")


def t_universe_failures_keep_protecting_positions():
    for failure in ("tickers", "instruments", "demo_instruments"):
        b = fake_bot(failure)
        result = run_cycle(b)
        assert_outage(b, result, 1)
        assert len(b.x.algos) == 1 and b.x.algos[0]["reduceOnly"] is True
        assert b.x.calls.index("guardrails") < b.x.calls.index("protect") < b.x.calls.index("tickers")
        assert any(e["msg"] == "holding" for e in b.log["events"])


def t_universe_failure_keeps_trailing_stops():
    b = fake_bot("instruments")
    b.x.pending_algos = Mock(return_value=[{"algoId": "old", "slTriggerPx": "90", "sz": "2"}])
    b.replace_sl = Mock(return_value=(True, 95))
    with patch.object(B, "update_stop", return_value=95):
        result = run_cycle(b)
    assert_outage(b, result, 1)
    b.replace_sl.assert_called_once()
    assert b.st["positions"][INST]["stop"] == 95
    assert any(e["msg"] == "trailed stop" for e in b.log["events"])


def t_spot_only_account_reconciles_and_protects_during_outage():
    b = fake_bot("tickers")
    spot_id = "TEST-USDT"
    b.st["positions"][spot_id] = b.st["positions"].pop(INST)
    b.swaps_blocked = True
    b.account = Mock(return_value=(1000, []))
    b.x.balance = Mock(return_value=[{"details": [{"ccy": "TEST", "eq": "2"}]}])
    b.x._req = Mock(side_effect=lambda method, path, params, **kw:
                    [{"last": "105"}] if path.endswith("ticker") else [dict(SPEC, instId=spot_id)])
    spot, guard, decision, equity_rows, _ = run_cycle(b)
    guard.assert_called_once()
    assert b.st["positions"][spot_id]["contracts"] == 2
    assert len(b.x.algos) == 1 and b.x.algos[0]["tdMode"] == "cash"
    assert "reduceOnly" not in b.x.algos[0]
    assert not b.x.orders and len(equity_rows) == 2
    assert any("universe fetch failed" in e for e in decision["errors"])
    assert not any(e.startswith("manage ") for e in decision["errors"])
    spot.enter_longs.assert_not_called()


def t_universe_failure_keeps_trend_exits():
    b = fake_bot("tickers")
    result = run_cycle(b, trend_exit=True)
    assert_outage(b, result, 1)
    assert b.x.closed == [INST]
    assert b.x.calls.index("close") < b.x.calls.index("tickers")


def t_universe_failure_keeps_daily_loss_and_drawdown_flattening():
    for equity, drawdown in ((890, False), (590, True)):
        b = fake_bot("instruments", equity)
        result = run_cycle(b)
        assert_outage(b, result, 0)
        assert b.x.closed == [INST]
        b.features.assert_not_called()
        assert result[0].manage_longs.call_args.args[0] is True
        assert result[4] is drawdown
        assert b.st.get("halted", False) is drawdown
        if not drawdown:
            assert b.st["day_locked"] is True


def t_success_still_allows_entries_after_management():
    b = fake_bot(None)
    spot, guard, decision, equity_rows, _ = run_cycle(b)
    guard.assert_called_once()
    spot.run_alloc.assert_called_once_with(can_rebuy=True)
    spot.enter_longs.assert_called_once()
    assert len(b.x.algos) == 1
    assert not any("universe fetch failed" in e for e in decision["errors"])
    assert any(e["msg"] == "universe" for e in b.log["events"])
    assert len(equity_rows) == 2


def t_account_failure_is_logged_and_ends_cycle():
    b = fake_bot("account")
    spot, guard, decision, equity_rows, _ = run_cycle(b)
    guard.assert_not_called()
    spot.manage_longs.assert_not_called()
    assert decision["errors"] == ["account fetch failed: account unavailable"]
    assert not equity_rows and b.x.calls == ["account"]


def t_spot_market_outage_does_not_skip_daily_flatten():
    b = fake_bot(None, 890)
    def fail_market(spot):
        spot.load_market.side_effect = B.OKXError("spot feed unavailable")
    spot, _, decision, _, _ = run_cycle(b, configure_spot=fail_market)
    assert b.st["day_locked"]
    assert spot.manage_longs.call_args.args[0] is True
    spot.run_alloc.assert_not_called()
    spot.enter_longs.assert_not_called()
    assert any("spot market" in e for e in decision["errors"])


def t_allocation_failure_does_not_skip_spot_management():
    b = fake_bot(None)
    def fail_allocation(spot):
        spot.run_alloc.side_effect = B.OKXError("allocation unavailable")
    spot, _, _, _, _ = run_cycle(b, configure_spot=fail_allocation)
    spot.manage_longs.assert_called_once()
    spot.enter_longs.assert_not_called()


def t_new_swap_is_excluded_from_same_cycle_spot_entries():
    b = fake_bot(None)
    new_inst = "NEW-USDT-SWAP"
    b.universe = Mock(return_value=[new_inst])
    frame = b.features.return_value.copy()
    frame["signal"], frame["strength"] = 1, 2
    b.features.return_value = frame
    b.enter = Mock(side_effect=lambda inst, *args: b.st["positions"].update({inst: {"side": 1}}))
    spot, _, _, _, _ = run_cycle(b)
    b.enter.assert_called_once()
    assert new_inst in spot.enter_longs.call_args.args[2]


def t_partial_swap_protection_repaired_without_price_change():
    b = fake_bot(None)
    b.x.algos = [{"instId": INST, "side": "sell", "posSide": "net", "sz": "1", "slTriggerPx": "90"}]
    with patch.object(B, "update_stop", return_value=90):
        run_cycle(b)
    assert len(b.x.algos) == 2 and float(b.x.algos[-1]["sz"]) == 1
    assert not b.x.closed and not getattr(b, "protection_failed", False)


def t_unconfirmed_stop_closes_and_blocks_entries():
    b = fake_bot(None)
    b.x.pending_algos = Mock(return_value=[])
    with patch.object(B.time, "sleep"):
        spot, _, decision, _, _ = run_cycle(b)
    assert b.protection_failed and b.x.closed == [INST]
    spot.enter_longs.assert_not_called()
    assert any("not confirmed" in e for e in decision["errors"])


def t_wrong_direction_stop_does_not_count_as_coverage():
    b = fake_bot(None)
    b.x.algos = [{"side": "buy", "posSide": "short", "sz": "2", "slTriggerPx": "110"}]
    with patch.object(B, "update_stop", return_value=90):
        run_cycle(b)
    assert len(b.x.algos) == 2 and b.x.algos[-1]["side"] == "sell"
    assert float(b.x.algos[-1]["sz"]) == 2


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("t_"):
            f(); n += 1; print("PASS", k)
    print(f"{n} tests passed")
