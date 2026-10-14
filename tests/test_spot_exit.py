"""Mocked spot exit regressions. Run: .venv/bin/python tests/test_spot_exit.py"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from okxbot.spot import SpotManager
from okxbot.okx_client import OKXError

INST = "TEST-USDT"


def fake_manager(qty=100, avail=100, filled=100, stop=9, last=12, tick="0.01"):
    x = SimpleNamespace(
        pending_algos=Mock(return_value=[{"algoId": "SL"}, {"algoId": "TP"}]),
        cancel_algos=Mock(), place_algo=Mock(),
        balance=Mock(return_value=[{"details": [{"ccy": "TEST", "availBal": str(avail), "eq": str(qty)}]}]),
    )
    b = SimpleNamespace(x=x, st={}, live=True, ev=Mock(), err=Mock())
    m = SpotManager(b)
    m.inst[INST] = {"lotSz": "0.1", "minSz": "1", "tickSz": tick}
    m.last[INST] = last
    m.st["longs"][INST] = {"qty": qty, "entry": 10, "stop": stop, "trade_id": "entry"}
    m.log = Mock()
    m.market = Mock(return_value={"sz": filled, "px": 12, "fee": -0.2, "feeCcy": "USDT", "ordId": "exit"})
    return m


def run_exit(m):
    with patch("okxbot.spot.J.record") as journal:
        m.exit_long(INST, "test")
    return journal


def assert_stop(m, qty, stop=9):
    assert m.st["longs"][INST]["qty"] == qty
    body = m.x.place_algo.call_args.args[0]
    assert body == {"instId": INST, "tdMode": "cash", "side": "sell", "ordType": "conditional",
                    "sz": str(float(qty)), "slTriggerPx": f"{stop:.2f}", "slOrdPx": "-1"}, body


def t_full_fill():
    m = fake_manager()
    j = run_exit(m)
    assert INST not in m.st["longs"]
    assert m.x.cancel_algos.call_count == 2
    m.x.place_algo.assert_not_called()
    assert m.log.call_args.kwargs["contracts"] == 100
    assert m.log.call_args.kwargs["pnl"] == 199.8
    assert j.call_args.kwargs["size"] == 100
    m.b.err.assert_not_called()


def t_partial_fill():
    m = fake_manager(filled=40)
    j = run_exit(m)
    m.market.assert_called_once_with(INST, "sell", 100.0, tag="x")
    assert_stop(m, 60)
    assert m.log.call_args.kwargs["contracts"] == 40
    assert m.log.call_args.kwargs["pnl"] == 79.8
    assert j.call_args.kwargs["size"] == 40
    assert j.call_args.kwargs["realized_pnl"] == 79.8


def t_available_balance_limits_sell():
    m = fake_manager(avail=40, filled=40)
    run_exit(m)
    m.market.assert_called_once_with(INST, "sell", 40.0, tag="x")
    assert_stop(m, 60)


def t_zero_fill():
    m = fake_manager(filled=0)
    j = run_exit(m)
    assert_stop(m, 100)
    m.log.assert_not_called()
    j.assert_not_called()


def t_below_min_sell_keeps_held_position():
    m = fake_manager(avail=0.5)
    j = run_exit(m)
    m.market.assert_not_called()
    assert_stop(m, 100)
    m.log.assert_not_called()
    j.assert_not_called()


def t_dust_dropped_with_event():
    for qty, filled in [(0.5, 0), (100, 99.5)]:
        m = fake_manager(qty=qty, filled=filled)
        run_exit(m)
        assert INST not in m.st["longs"]
        m.x.place_algo.assert_not_called()
        assert any(c.args[0] == "spot long dust dropped" for c in m.b.ev.call_args_list)
        if qty < 1:
            m.market.assert_not_called()
            m.log.assert_not_called()


def t_minimum_remainder_kept():
    m = fake_manager(filled=99)
    run_exit(m)
    assert_stop(m, 1)


def t_market_error_restores_stop():
    m = fake_manager()
    m.market.side_effect = OKXError("sell failed")
    j = run_exit(m)
    assert m.x.cancel_algos.call_count == 2
    assert_stop(m, 100)
    assert "sell failed" in m.b.err.call_args.args[0]
    m.log.assert_not_called()
    j.assert_not_called()


def t_restore_error_preserves_record_and_reports_both_errors():
    m = fake_manager()
    m.market.side_effect = OKXError("sell failed")
    m.x.place_algo.side_effect = OKXError("stop failed")
    run_exit(m)
    assert m.st["longs"][INST]["qty"] == 100
    assert m.b.err.call_count == 2
    assert "sell failed" in m.b.err.call_args_list[0].args[0]
    assert "stop failed" in m.b.err.call_args_list[1].args[0]


def t_marketable_stop_clamped_including_coarse_ticks():
    for tick in ("0.01", "1"):
        m = fake_manager(filled=0, stop=13, tick=tick)
        run_exit(m)
        body = m.x.place_algo.call_args.args[0]
        assert 0 < float(body["slTriggerPx"]) < 12
        assert m.st["longs"][INST]["stop"] == float(body["slTriggerPx"])


def t_dry_run_untouched():
    m = fake_manager()
    m.b.live = False
    j = run_exit(m)
    assert m.st["longs"][INST]["qty"] == 100
    m.x.pending_algos.assert_not_called()
    m.market.assert_not_called()
    m.x.place_algo.assert_not_called()
    j.assert_not_called()


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("t_"):
            f(); n += 1; print("PASS", k)
    print(f"{n} tests passed")
