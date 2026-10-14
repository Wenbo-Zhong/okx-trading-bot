"""Partial-fill / depth-cap tests. Run: cd tests && python3 test_fills.py"""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from okxbot import config
from okxbot import sizing
from okxbot import spot as S
from okxbot import bot as B
from test_sizing import fake_bot

def t_fill_summary_zro():
    n, partial = sizing.fill_summary(6943.85, 322.656, 2.3649569, quote=True)
    assert partial and abs(n - 763.07) < 0.05
    n, partial = sizing.fill_summary(100, 100, 2.0, ct_val=1)
    assert not partial and n == 200

def t_depth_cap():
    asks = [["2.36", "100"], ["2.37", "100"], ["2.40", "10000"]]
    assert abs(sizing.depth_cap_usdt(asks, 2.36, 0.01) - (236 + 237) * 0.8) < 1e-6

class FX:
    def __init__(self, fill_sz, avg, state="canceled", asks=None):
        self.fill_sz, self.avg, self.state, self.algos = fill_sz, avg, state, []
        self.asks = asks or [["2.36", "100000"]]
    def place_order(self, b): self.req = b; return [{"ordId": "1"}]
    def get_order(self, i, o): return [{"state": self.state, "accFillSz": str(self.fill_sz), "avgPx": str(self.avg),
                                        "fee": str(-self.fill_sz * 0.001), "feeCcy": "ZRO", "cancelSource": "15"}]
    def max_size(self, i, m, px=None): return [{"maxBuy": "1e9"}]
    def place_algo(self, b): self.algos.append(b); return [{}]
    def _req(self, m, p, params=None, **k): return [{"asks": self.asks, "bids": [["2.35", "1e9"]]}]
    def balance(self): return [{"details": [{"ccy": "USDT", "availBal": "60000", "eq": "60000"}]}]

def mk(fx):
    b = types.SimpleNamespace(liquidity_check=lambda i, s, px, st, n, c, market="spot": (n, True, {}), live=True, usdt_avail=60000.0, log={}, size_mult=1.0, mode="test", ctx={})
    evs = []; b.ev = lambda m, **k: evs.append((m, k)); b.err = lambda m: evs.append(("ERR", m))
    b.st = {"spot": {}}; b.x = fx
    sm = S.SpotManager(b); sm.inst = {"ZRO-USDT": {"tickSz": "0.0001", "lotSz": "0.001", "minSz": "1", "maxMktAmt": "73000"}}
    sm.last = {"ZRO-USDT": 2.36}
    S.time.sleep = lambda s: None
    logged = []; sm.log = lambda **r: logged.append(r)
    return sm, evs, logged

def t_spot_partial_fill_sizes_stop_off_actual_qty():
    fx = FX(322.656, 2.3649569)
    sm, evs, logged = mk(fx)
    sm.open_long("ZRO-USDT", 0.0576, 0, 100000)
    assert any(e[0] == "PARTIAL FILL" for e in evs)
    assert float(fx.algos[0]["sz"]) == 322.333                  # OCO covers net filled qty, not requested
    assert logged[0]["notional"] == round(322.333 * 2.3649569, 2) and "PARTIAL FILL" in logged[0]["reason"]
    rec = sm.st["longs"]["ZRO-USDT"]
    assert rec["qty"] == 322.333 and abs(rec["planned_risk_usdt"] - (2.3649569 - rec["init_stop"]) * 322.333) < 1e-3

def t_depth_cap_limits_order():
    fx = FX(150, 2.36, "filled", asks=[["2.36", "200"], ["2.50", "1e6"]])
    sm, evs, _ = mk(fx)
    sm.open_long("ZRO-USDT", 0.0576, 0, 100000)
    assert float(fx.req["sz"]) <= 2.36 * 200 * 0.8 + 0.01, fx.req

def t_routed_thin_book_falls_back_to_swap():
    fx = FX(0, 0, asks=[["2.36", "50"]])
    sm, evs, _ = mk(fx)
    assert sm.open_long("ZRO-USDT", 0.0576, 0, 100000, routed_from="ZRO-USDT-SWAP") == "fallback"
    config.LIQMAP_STOP_NUDGE = False
    b, _ = fake_bot(5000, None)
    b.spot = types.SimpleNamespace(inst={"ZRO-USDT": {}}, open_long=lambda *a, **k: "fallback")
    b.enter("ZRO-USDT-SWAP", 1, 0.06, 0, 100000, 0)
    assert b.x.orders and b.x.orders[0]["side"] == "buy" and b.x.orders[0]["attachAlgoOrds"][0]["slTriggerPx"]

if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("t_"):
            f(); n += 1; print("PASS", k)
    print(f"{n} tests passed")
