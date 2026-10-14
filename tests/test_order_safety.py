"""Offline duplicate-order, persistence and run lock regressions."""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from okxbot import bot as B, config
from okxbot.okx_client import OKX, OKXError
from requests.exceptions import Timeout
from test_sizing import fake_bot
from test_fills import FX, mk
from test_spot_exit import fake_manager, INST

ORDER = {"instId": "TEST-USDT", "clOrdId": "sameOrder123", "side": "buy", "sz": "1"}


def response(data=None, code="0", status=200):
    return Mock(status_code=status, json=Mock(return_value={"code": code, "data": data or []}))


def client(outcomes):
    session = Mock(headers={}, request=Mock(side_effect=outcomes))
    with patch("okxbot.okx_client.requests.Session", return_value=session):
        x = OKX()
    x.key = x.secret = x.passphrase = "fake"
    return x


def must_fail(call):
    try:
        call()
    except OKXError:
        return
    raise AssertionError("expected OKXError")


def t_timeout_lookup_found_does_not_repost():
    x = client([Timeout(), response([{"ordId": "42", "state": "filled"}])])
    with patch("okxbot.okx_client.time.sleep"):
        assert x.place_order(ORDER) == [{"ordId": "42", "clOrdId": ORDER["clOrdId"], "sCode": "0", "sMsg": ""}]
    calls = x.s.request.call_args_list
    assert [c.args[0] for c in calls] == ["POST", "GET"]
    assert calls[1].args[1].endswith("/api/v5/trade/order?instId=TEST-USDT&clOrdId=sameOrder123")
    assert all(c.kwargs["headers"]["x-simulated-trading"] == "1" for c in calls)


def t_timeout_not_found_retries_once_with_same_id():
    for missing in (response(), response(code="51603")):
        x = client([Timeout(), missing, response([{"ordId": "42"}])])
        with patch("okxbot.okx_client.time.sleep") as sleep:
            assert x.place_order(ORDER) == [{"ordId": "42"}]
        calls = x.s.request.call_args_list
        assert [c.args[0] for c in calls] == ["POST", "GET", "POST"]
        assert calls[0].kwargs["data"] == calls[2].kwargs["data"] == json.dumps(ORDER, separators=(",", ":"))
        sleep.assert_called_once_with(1.5)


def t_second_timeout_is_reconciled_but_never_reposted_again():
    for found in (False, True):
        x = client([Timeout(), response(), Timeout(), response([{"ordId": "42"}] if found else [])])
        with patch("okxbot.okx_client.time.sleep"):
            if found:
                assert x.place_order(ORDER)[0]["ordId"] == "42"
            else:
                must_fail(lambda: x.place_order(ORDER))
        assert [c.args[0] for c in x.s.request.call_args_list] == ["POST", "GET", "POST", "GET"]


def t_lookup_failure_does_not_repost():
    x = client([Timeout(), response(code="50101")])
    with patch("okxbot.okx_client.time.sleep"):
        must_fail(lambda: x.place_order(ORDER))
    assert x.s.request.call_count == 2


def t_other_posts_and_orders_without_id_do_not_retry_exceptions():
    for path, body in (("/api/v5/trade/order", {"instId": "TEST-USDT"}),
                       ("/api/v5/trade/cancel-order", {}), ("/api/v5/trade/amend-algos", {}),
                       ("/api/v5/trade/cancel-algos", []), ("/api/v5/trade/order-algo", {}),
                       ("/api/v5/trade/close-position", {})):
        x = client([Timeout()])
        with patch("okxbot.okx_client.time.sleep") as sleep:
            must_fail(lambda: x._req("POST", path, body=body, private=True))
        assert x.s.request.call_count == 1
        sleep.assert_not_called()


def t_posts_still_retry_rate_limits():
    for limited in (Mock(status_code=429, json=Mock(side_effect=ValueError())), response(code="50011")):
        x = client([limited, response([{"ordId": "42"}])])
        with patch("okxbot.okx_client.time.sleep"):
            assert x.cancel_order("TEST-USDT", "42") == [{"ordId": "42"}]
        assert x.s.request.call_count == 2


def t_get_retries_unchanged():
    for private in (False, True):
        x = client([Timeout(), Timeout(), response([{"ordId": "42"}])])
        with patch("okxbot.okx_client.time.sleep") as sleep:
            assert x._req("GET", "/api/v5/trade/order", private=private) == [{"ordId": "42"}]
        assert [c.args[0] for c in x.s.request.call_args_list] == ["GET"] * 3
        assert [c.args[0] for c in sleep.call_args_list] == [1.5, 3.0]
    x = client([Timeout()] * 3)
    with patch("okxbot.okx_client.time.sleep"):
        must_fail(lambda: x.tickers())
    assert x.s.request.call_count == 3


def t_swap_signal_saved_before_fill_lookup_failure():
    b, _ = fake_bot(620)
    b.x.place_order = Mock(return_value=[{"ordId": "42"}])
    b.x.get_order = Mock(side_effect=OKXError("fill lookup failed"))
    with tempfile.TemporaryDirectory() as tmp, patch.object(B, "STATE_F", os.path.join(tmp, "state.json")), \
            patch.object(config, "LIQMAP_STOP_NUDGE", False), patch.object(B.time, "sleep"):
        b.enter("ZRO-USDT-SWAP", 1, 0.06, 123, 100000, 0)
        with open(B.STATE_F) as f:
            assert json.load(f)["last_signal_ts"]["ZRO-USDT-SWAP"] == 123


def t_spot_position_saved_before_protection_failure():
    sm, _, _ = mk(FX(150, 2.36, "filled"))
    sm.x.place_algo = Mock(side_effect=RuntimeError("process interrupted"))
    with tempfile.TemporaryDirectory() as tmp, patch.object(B, "STATE_F", os.path.join(tmp, "state.json")), \
            patch.object(B.J, "record"):
        try:
            sm.open_long("ZRO-USDT", 0.0576, 123, 100000)
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected interruption")
        with open(B.STATE_F) as f:
            saved = json.load(f)
        assert saved["spot"]["longs"]["ZRO-USDT"]["qty"] > 0
        assert saved["spot"]["last_signal_ts"]["ZRO-USDT"] == 123


def t_spot_close_persists_remaining_and_removed_position():
    for filled, remaining in ((40, 60), (100, 0)):
        m = fake_manager(filled=filled)
        with tempfile.TemporaryDirectory() as tmp, patch.object(B, "STATE_F", os.path.join(tmp, "state.json")), \
                patch.object(B.J, "record"):
            m.exit_long(INST, "test")
            with open(B.STATE_F) as f:
                longs = json.load(f)["spot"]["longs"]
            assert longs[INST]["qty"] == remaining if remaining else INST not in longs


def t_state_replace_is_atomic_and_temp_file_is_closed():
    with tempfile.TemporaryDirectory() as tmp, patch.object(B, "STATE_F", os.path.join(tmp, "state.json")):
        B.save_state({"old": True})
        original_replace = os.replace
        def replace(src, dst):
            with open(dst) as f:
                assert json.load(f) == {"old": True}
            with open(src) as f:
                assert json.load(f) == {"new": True}
            original_replace(src, dst)
        with patch.object(B.os, "replace", side_effect=replace) as replaced:
            B.save_state({"new": True})
            replaced.assert_called_once()
        with open(B.STATE_F) as f:
            assert json.load(f) == {"new": True}


def t_lock_held_second_process_exits_three_without_trading():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    program = """
import runpy, sys
from unittest.mock import patch
from okxbot import config
config.DATA_DIR = sys.argv[1]
with patch('okxbot.bot.Bot', side_effect=AssertionError('must not construct bot')), \\
     patch('requests.sessions.Session.request', side_effect=AssertionError('no network')):
    runpy.run_path('scripts/run_once.py', run_name='__main__')
"""
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "run_once.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run([sys.executable, "-c", program, tmp], cwd=root,
                                    capture_output=True, text=True, timeout=20)
        assert result.returncode == 3, (result.stdout, result.stderr)
        assert "ALERTS:\n- [error] Another run_once instance is running" in result.stdout
        assert "skipping trading" in result.stdout and not result.stderr


if __name__ == "__main__":
    n = 0
    for name, test in list(globals().items()):
        if name.startswith("t_"):
            test(); n += 1; print("PASS", name)
    print(f"{n} tests passed")
