"""Unit + mocked-integration tests for spot preference and pre-order clamps. Run: python3 tests/test_sizing.py"""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config, sizing, risk
import bot as B

def t_prefer():
    assert sizing.prefer_spot(1, 1, True, False)
    assert not sizing.prefer_spot(-1, 1, True, False)        # shorts stay swaps
    assert not sizing.prefer_spot(1, 2, True, False)         # genuinely levered
    assert not sizing.prefer_spot(1, 1, False, False)        # no spot pair
    assert not sizing.prefer_spot(1, 1, True, True)          # tradfi (XAU/QQQ)

def t_lev_from_sizing():
    eq = 100000
    _, lev, _ = risk.size_position(eq, 2.32, 2.32 * (1 - 0.08))   # wide stop -> small notional -> 1x
    assert lev == 1
    _, lev, _ = risk.size_position(eq, 100, 99.5)                 # tight stop -> levered
    assert lev > 1

def t_clamp_swap_zro():
    # ZRO 51004 case: risk size 3029 ct, exchange max-size 620 at 1x
    c, n = sizing.clamp_swap(3029, 1, 1, 1, 1, 2.32, 60000, exch_max_ct=620, max_mkt_sz=100000)
    assert c == 589 and "exchange max-size/tier" in n[0], (c, n)

def t_clamp_swap_grass():
    # GRASS 51008 case: margin needed > available USDT
    c, n = sizing.clamp_swap(20000, 1, 1, 2, 1, 0.84, 3000, exch_max_ct=50000)
    assert c == int(3000 * .95 * 2 / .84) and "margin" in n[0], (c, n)
    c, n = sizing.clamp_swap(20000, 1, 1, 2, 1, 0.84, 0.1, exch_max_ct=50000)
    assert c == 0 and "skip" in n[-1]

def t_clamp_swap_untouched():
    c, n = sizing.clamp_swap(100, 1, 1, 3, 1, 10, 1e6, exch_max_ct=10000, max_mkt_sz=100000)
    assert c == 100 and not n
    c, n = sizing.clamp_swap(12.37, 0.1, 0.1, 3, 1, 10, 1e6)
    assert c == 12.3

def t_clamp_spot():
    u, n = sizing.clamp_spot(15000, 62000, 5.8, exch_max_base=10700, max_mkt_amt=73000)
    assert u == 15000 and not n
    u, n = sizing.clamp_spot(15000, 4000, 5.8)
    assert u == 3800 and "available USDT" in n[0]
    u, n = sizing.clamp_spot(15000, 5, 5.8)
    assert u == 0
    u, n = sizing.clamp_spot(100000, 1e6, 1.0, max_mkt_amt=73000)
    assert u == 69350

class FakeX:
    def __init__(self, max_buy, venue_ct_val="1"): self.max_buy, self.orders, self.lev, self.venue_ct_val = max_buy, [], [], venue_ct_val
    book = {"asks": [["2.321", "1e9"]], "bids": [["2.319", "1e9"]]}   # deep by default
    def _req(self, m, p, params=None, **k):
        if "books" in p: return [self.book]
        return [{"ctVal": self.venue_ct_val, "ctMult": "1"}]
    def set_leverage(self, i, l, p=None): self.lev.append(l)
    def max_size(self, i, m, px=None): return [{"maxBuy": str(self.max_buy), "maxSell": str(self.max_buy)}]
    def place_order(self, o): self.orders.append(o); raise B.OKXError("stop after capture")

def fake_bot(max_buy, spot_inst=None):
    b = B.Bot.__new__(B.Bot)
    b.st = {"positions": {}, "last_signal_ts": {}, "spot": {"longs": {}}}
    b.log, b.live, b.swaps_blocked, b.inst_type, b.pos_mode, b.size_mult = {}, True, False, "SWAP", "long_short_mode", 1.0
    b.x, b.usdt_avail, b.errors, b.mode = FakeX(max_buy), 60000.0, [], "test"
    b.ev = lambda msg, **k: b.log.setdefault("ev", []).append((msg, k))
    b.err = lambda m: b.errors.append(m)
    b.instr = {"ZRO-USDT-SWAP": {"tickSz": "0.0001", "lotSz": "1", "minSz": "1", "ctVal": "1", "maxMktSz": "100000"}}
    b.last_px = {"ZRO-USDT-SWAP": 2.32}
    calls = []
    if spot_inst:
        b.spot = types.SimpleNamespace(inst={spot_inst: {}}, open_long=lambda *a, **k: calls.append((a, k)) or True)
    else:
        b.spot = None
    return b, calls

def t_bot_routes_1x_long_to_spot():
    config.LIQMAP_STOP_NUDGE = False
    b, calls = fake_bot(620, "ZRO-USDT")
    b.enter("ZRO-USDT-SWAP", 1, 0.06, 0, 100000, 0)
    assert calls and calls[0][1]["routed_from"] == "ZRO-USDT-SWAP" and not b.x.orders, (calls, b.x.orders)

def t_bot_clamps_swap_without_spot():
    config.LIQMAP_STOP_NUDGE = False
    b, _ = fake_bot(620, None)
    b.enter("ZRO-USDT-SWAP", 1, 0.06, 0, 100000, 0)
    assert b.x.orders and float(b.x.orders[0]["sz"]) <= 620, b.x.orders
    assert "attachAlgoOrds" in b.x.orders[0] and b.x.orders[0]["attachAlgoOrds"][0]["slTriggerPx"]   # SL still attached

def t_bot_short_stays_swap():
    config.LIQMAP_STOP_NUDGE = False
    b, calls = fake_bot(620, "ZRO-USDT")
    b.enter("ZRO-USDT-SWAP", -1, 0.06, 0, 100000, 0)
    assert not calls and b.x.orders and b.x.orders[0]["side"] == "sell"

def t_bot_skip_when_cap_zero():
    config.LIQMAP_STOP_NUDGE = False
    b, _ = fake_bot(0, None)
    b.enter("ZRO-USDT-SWAP", -1, 0.06, 0, 100000, 0)
    assert not b.x.orders and any("skip" in e[0] for e in b.log["ev"])

if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("t_"):
            f(); n += 1; print("PASS", k)
    print(f"{n} tests passed")
