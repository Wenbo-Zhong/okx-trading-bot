"""Contract-spec / order sanity tests (LIT 10x incident). Run: cd tests && python3 test_contracts.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config, sizing
from test_sizing import fake_bot

def t_merge_uses_demo_specs():
    prod = {"LIT-USDT-SWAP": {"instId": "LIT-USDT-SWAP", "ctVal": "1", "lotSz": "1", "state": "live"}}
    demo = {"LIT-USDT-SWAP": {"ctVal": "10", "ctMult": "1", "lotSz": "1"}}
    m = sizing.merge_exec_specs(prod, demo)["LIT-USDT-SWAP"]
    assert m["ctVal"] == "10" and m["_spec_src"] == "demo" and m["state"] == "live"

def t_sanity_lit_incident():
    # plan 6363 notional / 423 risk; order of 1790 ct at real ctVal 10 -> 63.6k notional -> refuse
    ok, info = sizing.order_sanity(1790, "10", "1", 3.5546, 3.7912, 6363, 423)
    assert not ok and info["order_notional"] > 60000
    ok, _ = sizing.order_sanity(179, "10", "1", 3.5546, 3.7912, 6363, 423.5)
    assert ok
    ok, _ = sizing.order_sanity(179, "10", "10", 3.5546, 3.7912, 6363, 423.5)   # ctMult respected
    assert not ok

def t_bot_refuses_when_venue_ctval_differs():
    config.LIQMAP_STOP_NUDGE = False
    b, _ = fake_bot(100000, None)
    b.x.venue_ct_val = "10"          # instr says 1 (prod spec), venue says 10
    b.enter("ZRO-USDT-SWAP", -1, 0.06, 0, 100000, 0)
    assert not b.x.orders and b.log.get("refused_orders"), b.log

def t_bot_sized_off_demo_spec_passes():
    config.LIQMAP_STOP_NUDGE = False
    b, _ = fake_bot(100000, None)
    b.instr["ZRO-USDT-SWAP"]["ctVal"] = "10"; b.x.venue_ct_val = "10"
    b.enter("ZRO-USDT-SWAP", -1, 0.06, 0, 100000, 0)
    o = b.x.orders[0]
    ok, info = sizing.order_sanity(o["sz"], "10", "1", 2.32, 2.32 + 0.06 * 2, 1e18, 1e18)
    assert info["order_risk"] <= 100000 * config.RISK_PER_TRADE * 1.2 + 1, info   # risk ≈ plan, not 10x

if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("t_"):
            f(); n += 1; print("PASS", k)
    print(f"{n} tests passed")
