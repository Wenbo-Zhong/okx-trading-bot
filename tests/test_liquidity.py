"""Liquidity filter + stop-slippage buffer tests. Run: cd tests && python3 test_liquidity.py"""
import os, sys
import tempfile
os.environ.setdefault("OKXBOT_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from okxbot import config
from okxbot import sizing
from test_sizing import fake_bot

DEEP = {"asks": [["100.01", "1e6"]], "bids": [["99.99", "1e6"]]}

def t_floor_only_on_deep_book():
    # stop 2% away, floor 0.5% -> qty shrinks by 2/(2+0.5)=0.8
    n, info, ok = sizing.liquidity_size(DEEP, 1, 100, 98, 10000, 1.0, 0.005)
    assert ok and abs(n - 10000 * 2 / (2 + 0.49)) < 1 and abs(info["risk_incl_slip"] - 200) < 1, info

def t_book_impact_dominates_on_thin_book():
    # LIT-like: stop side (asks for a short) thin -> large impact -> bigger shrink than the floor
    book = {"asks": [["3.56", "100"], ["3.70", "200"], ["4.10", "1e6"]], "bids": [["3.55", "1e6"]]}
    n, info, ok = sizing.liquidity_size(book, -1, 3.555, 3.79, 6363, 1.0, 0.01)
    assert info["est_stop_slip_pct"] > 1 and n < 6363 * 0.235 / (0.235 + 0.0379) + 1, info
    assert abs(info["risk_incl_slip"] - info["planned_risk"]) < 1        # risk incl slippage == plan
    assert not ok                                                         # and depth within 1.5% too thin -> skip

def t_skip_when_entry_side_thin():
    book = {"asks": [["100.01", "1"]], "bids": [["99.99", "1e6"]]}        # long entry side thin
    _, info, ok = sizing.liquidity_size(book, 1, 100, 98, 10000, 1.0, 0.005)
    assert not ok and info["depth_entry_usdt"] < 1000

def t_contract_units():
    book = {"asks": [["100.01", "100"]], "bids": [["99.99", "100"]]}      # 100 ct x 10 coins = ~100k
    _, info, ok = sizing.liquidity_size(book, 1, 100, 98, 10000, 10.0, 0.005)
    assert ok and info["depth_stop_usdt"] > 90000

def t_bot_skips_thin_book_and_journals():
    from okxbot import journal_log as J
    rec = []; J.record = lambda ev, **k: rec.append((ev, k))
    config.LIQMAP_STOP_NUDGE = False
    b, _ = fake_bot(100000, None)
    b.x.book = {"asks": [["2.321", "10"]], "bids": [["2.319", "10"]]}
    b.enter("ZRO-USDT-SWAP", -1, 0.06, 0, 100000, 0)
    assert not b.x.orders and any(e[0] == "skipped: thin book" for e in b.log["ev"])
    assert rec and rec[0][0] == "skip" and rec[0][1]["reason"] == "thin book"

def t_bot_shrinks_for_slippage_and_keeps_sl():
    config.LIQMAP_STOP_NUDGE = False
    b, _ = fake_bot(1e9, None)
    b.enter("ZRO-USDT-SWAP", -1, 0.06, 0, 100000, 0)
    o = b.x.orders[0]
    li = b.log["liquidity"][0]
    assert li["notional_after_slip"] < li["planned_notional"] and o["attachAlgoOrds"][0]["slTriggerPx"]
    assert float(o["sz"]) * 2.32 <= li["notional_after_slip"] + 2.32

if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("t_"):
            f(); n += 1; print("PASS", k)
    print(f"{n} tests passed")
