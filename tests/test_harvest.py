"""Funding-harvest exception tests. Run: python3 tests/test_harvest.py

Default config has FUNDING_HARVEST_ENABLED=False (spot prefer only). Tests that need the
harvest path temporarily force the flag on and restore it afterward.
"""
import os, sys
import tempfile
os.environ.setdefault("OKXBOT_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from okxbot import config
from okxbot import sizing
from okxbot import funding
from okxbot import bot as B
from test_sizing import fake_bot

def t_rule():
    assert sizing.funding_harvest(-0.0003, -0.0001)
    assert not sizing.funding_harvest(-0.0001, None)          # not meaningful
    assert not sizing.funding_harvest(-0.0003, 0.0002)        # last settled positive -> not persistent
    assert not sizing.funding_harvest(None)

def t_flip():
    c, a = 0, None
    for _ in range(5):
        c, a = sizing.harvest_flip_action(0.0002, c)
    assert c == 5 and a is None
    c, a = sizing.harvest_flip_action(0.0002, c); assert a == "flag"
    c, a = sizing.harvest_flip_action(-0.0001, c); assert c == 0 and a is None   # reset
    c = 5; c, a = sizing.harvest_flip_action(0.0006, c); assert a == "close"

def run_enter(rate, side=1):
    config.LIQMAP_STOP_NUDGE = False
    b, calls = fake_bot(5000, "ZRO-USDT")
    b.funding = {"ZRO-USDT-SWAP": {"rate_8h": rate}}
    funding.last_settled = lambda i: rate
    b.enter("ZRO-USDT-SWAP", side, 0.06, 0, 100000, 0)
    return b, calls

def t_negative_funding_keeps_swap():
    """Harvest path still works when the flag is on."""
    prev = config.FUNDING_HARVEST_ENABLED
    config.FUNDING_HARVEST_ENABLED = True
    try:
        b, calls = run_enter(-0.0004)
        assert not calls and b.x.orders and b.x.orders[0]["side"] == "buy", (calls, b.x.orders)
        assert any("funding harvest" in e[0] for e in b.log["ev"])
        assert b.x.lev == [1] and b.x.orders[0]["attachAlgoOrds"][0]["slTriggerPx"]      # 1x, SL attached
    finally:
        config.FUNDING_HARVEST_ENABLED = prev

def t_harvest_disabled_goes_spot():
    """Default (flag off): even deeply negative funding routes 1x longs to spot."""
    assert config.FUNDING_HARVEST_ENABLED is False
    b, calls = run_enter(-0.0004)
    assert calls and not b.x.orders, (calls, b.x.orders)
    assert not any("funding harvest" in e[0] for e in b.log["ev"])

def t_small_or_positive_funding_goes_spot():
    for r in (-0.00005, 0.0003):
        b, calls = run_enter(r)
        assert calls and not b.x.orders

def t_manage_flag_and_close():
    b, _ = fake_bot(1, None)
    closed = []
    b.close = lambda i, ps, why: closed.append((i, why))
    b.pos_side = lambda s: "long"
    b.st["positions"] = {"ZRO-USDT-SWAP": {"intent": "funding_harvest", "fund_flip_count": 5},
                         "ATOM-USDT-SWAP": {"intent": "trend"}}
    b.funding = {"ZRO-USDT-SWAP": {"rate_8h": 0.0002}, "ATOM-USDT-SWAP": {"rate_8h": 0.01}}
    b.manage_funding_harvest()
    assert not closed and b.log["funding_harvest_flags"][0]["cycles_positive"] == 6
    b.funding["ZRO-USDT-SWAP"]["rate_8h"] = 0.0006
    b.manage_funding_harvest()
    assert closed == [("ZRO-USDT-SWAP", "funding_flip")]   # trend positions untouched

def t_journal_labels():
    from okxbot import journal
    assert journal.INTENT_ZH["funding_harvest"] == "吃资金费率"
    assert journal.classify_exit({"side": "long"}, {"reason": "funding_flip"}, [], []) == "funding_flip"

if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("t_"):
            f(); n += 1; print("PASS", k)
    print(f"{n} tests passed")
