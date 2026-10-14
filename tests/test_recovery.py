"""Offline crash recovery and exchange partial-exit regressions."""
import json
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("OKXBOT_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from okxbot import bot as B, spot as S
from okxbot.okx_client import OKXError
from test_fills import FX, mk

INST = "ZRO-USDT"


class Exchange(FX):
    def __init__(self):
        super().__init__(100, 2.36, "filled")
        self.orders = []
        self.lookups_fail = False

    def place_order(self, body):
        self.orders.append(body)
        return super().place_order(body)

    def get_order(self, inst, order):
        if self.lookups_fail:
            raise OKXError("order lookup unavailable")
        return super().get_order(inst, order)

    def place_algo(self, body):
        self.algos.append(dict(body, algoId=str(len(self.algos) + 1)))
        return [{"algoId": self.algos[-1]["algoId"], "sCode": "0"}]

    def pending_algos(self, *args):
        return self.algos

    def get_algo(self, algo_id):
        return [{"ordIdList": []}]


def t_buy_lookup_failure_survives_restart_and_gets_protection():
    fx = Exchange()
    m, _, _ = mk(fx)
    fx.lookups_fail = True
    with tempfile.TemporaryDirectory() as tmp, patch.object(B, "STATE_F", os.path.join(tmp, "state.json")), patch.object(S.J, "record"):
        assert m.open_long(INST, 0.0576, 123, 100000) is False
        saved = json.load(open(B.STATE_F))
        pending = next(iter(saved["spot"]["pending_orders"].values()))
        assert pending["ordId"] == "1" and pending["body"]["clOrdId"]
        assert m.open_long(INST, 0.0576, 124, 100000) is False
        assert len(fx.orders) == 1
        m.b.st = saved
        restarted = S.SpotManager(m.b)
        assert restarted.recover_pending() is False
        fx.lookups_fail = False
        assert restarted.recover_pending() is True
        qty = restarted.st["longs"][INST]["qty"]
        assert qty == 99.9  # base-currency entry fee
        restarted.protect_long(INST)
        assert float(fx.algos[-1]["sz"]) == qty
        assert restarted.recover_pending() is True
        assert len(fx.orders) == 1 and restarted.st["longs"][INST]["qty"] == qty


def t_intent_saved_before_post_and_recovered_by_client_id():
    fx = Exchange()
    m, _, _ = mk(fx)
    with tempfile.TemporaryDirectory() as tmp, patch.object(B, "STATE_F", os.path.join(tmp, "state.json")), patch.object(S.J, "record"):
        def interrupted(body):
            saved = json.load(open(B.STATE_F))
            assert body["clOrdId"] in saved["spot"]["pending_orders"]
            raise OKXError("POST response lost")
        fx.place_order = Mock(side_effect=interrupted)
        assert m.open_long(INST, 0.0576, 123, 100000) is False
        key = next(iter(m.st["pending_orders"]))
        fx._req = Mock(return_value=[dict(fx.get_order(INST, "1")[0], ordId="1")])
        assert m.recover_pending()
        params = fx._req.call_args.args[2]
        assert params == {"instId": INST, "clOrdId": key}
        assert fx.place_order.call_count == 1


def t_explicit_buy_rejection_removes_pending_intent():
    fx = Exchange()
    fx.place_order = Mock(return_value=[{"ordId": "", "sCode": "51008"}])
    m, _, _ = mk(fx)
    assert m.open_long(INST, 0.0576, 123, 100000) is False
    assert not m.st["pending_orders"] and not m.st["longs"]


def t_recovery_cancels_live_buy_before_registering_final_size():
    fx = Exchange()
    m, _, _ = mk(fx)
    plan = {"atr": 0.05, "bar_ts": 123, "stop": 2.2, "tp": 2.7, "spec": m.inst[INST]}
    m.st["pending_orders"]["client1"] = {"body": {"instId": INST}, "ordId": "1", "plan": plan}
    live = {"state": "partially_filled", "accFillSz": "40", "avgPx": "2.36"}
    final = dict(live, state="canceled", accFillSz="60")
    fx.get_order = Mock(side_effect=[[live], [final]])
    fx.cancel_order = Mock()
    assert m.recover_pending()
    fx.cancel_order.assert_called_once_with(INST, "1")
    assert m.st["longs"][INST]["qty"] == 60


def partial_manager():
    fx = Exchange()
    m, _, _ = mk(fx)
    m.st["longs"][INST] = {"qty": 100, "entry": 2, "stop": 1.8, "init_stop": 1.8,
                             "entry_ts": 0, "trade_id": "entry", "algo_ids": ["executed"]}
    fx.get_algo = Mock(return_value=[{"ordIdList": ["sell1"]}])
    fx.get_order = Mock(return_value=[{"state": "canceled", "accFillSz": "60", "avgPx": "2.5", "fee": "-0.1", "feeCcy": "USDT"}])
    return m, fx


def t_exchange_partial_exit_keeps_remainder_and_applies_fills_once():
    m, fx = partial_manager()
    with patch.object(S.J, "record") as journal:
        assert m.reconcile_long(INST, {"eq": 40})
        assert m.st["longs"][INST]["qty"] == 40
        event = journal.call_args.kwargs
        assert event["size"] == 60 and event["remaining"] == 40
        assert abs(event["realized_pnl"] - 29.9) < 1e-9
        assert m.reconcile_long(INST, {"eq": 40})
        assert journal.call_count == 1
        m.protect_long(INST)
    assert float(fx.algos[-1]["sz"]) == 40


def t_legacy_balance_drop_does_not_fabricate_full_close():
    fx = Exchange()
    m, _, _ = mk(fx)
    m.st["longs"][INST] = {"qty": 100, "entry": 2, "stop": 1.8}
    with patch.object(S.J, "record") as journal:
        assert m.reconcile_long(INST, {"eq": 40})
        assert m.st["longs"][INST]["qty"] == 40
        journal.assert_not_called()
        m.protect_long(INST)
    assert float(fx.algos[-1]["sz"]) == 40


def t_delayed_exit_lookup_does_not_subtract_balance_gap_twice():
    m, fx = partial_manager()
    fx.get_algo.side_effect = OKXError("temporary lookup failure")
    with patch.object(S.J, "record") as journal:
        assert m.reconcile_long(INST, {"eq": 40})
        assert m.st["longs"][INST]["unreconciled_qty"] == 60
        journal.assert_not_called()
        fx.get_algo.side_effect = None
        m.b.protection_failed = False
        assert m.reconcile_long(INST, {"eq": 40})
        assert m.st["longs"][INST]["qty"] == 40
        assert m.st["longs"][INST]["unreconciled_qty"] == 0
        assert journal.call_count == 1 and not m.b.protection_failed


def t_targeted_specs_allow_emergency_exit_without_bulk_market():
    fx = Exchange()
    m, _, _ = mk(fx)
    spec = m.inst.pop(INST)
    m.st["longs"][INST] = {"qty": 100, "entry": 2, "stop": 1.8, "spec": spec}
    m.last[INST] = 2.36
    fx.fill_sz = 100
    fx.balance = Mock(return_value=[{"details": [{"ccy": "ZRO", "availBal": "100", "eq": "100"}]}])
    m.exit_long(INST, "guardrail")
    assert INST not in m.st["longs"] and fx.orders[0]["side"] == "sell"


if __name__ == "__main__":
    with patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")):
        tests = [fn for name, fn in list(globals().items()) if name.startswith("t_")]
        for test in tests:
            test()
            print("PASS", test.__name__)
        print(f"{len(tests)} tests passed")
