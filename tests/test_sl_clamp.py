"""Stop-loss live last/mark clamp + soft amend skip. Run: python3 tests/test_sl_clamp.py"""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import bot as B


def t_short_clamp_below_mark():
    # AAVE-like: trail below live mark -> clamp above max(last, mark)
    stop, clamped, reason = B.clamp_sl_trigger(-1, 159.85, last_px=160.44, mark_px=160.36, buffer_pct=0.001)
    assert clamped and stop > 160.44 and "short SL" in reason
    assert abs(stop - 160.44 * 1.001) < 1e-9


def t_short_valid_unchanged():
    stop, clamped, _ = B.clamp_sl_trigger(-1, 167.43, last_px=160.44, mark_px=160.36)
    assert not clamped and stop == 167.43


def t_long_clamp_above_mark():
    stop, clamped, reason = B.clamp_sl_trigger(1, 101.0, last_px=100.0, mark_px=100.2, buffer_pct=0.001)
    assert clamped and stop < 100.0 and "long SL" in reason
    assert abs(stop - 100.0 * 0.999) < 1e-9


def t_long_valid_unchanged():
    stop, clamped, _ = B.clamp_sl_trigger(1, 95.0, last_px=100.0, mark_px=100.2)
    assert not clamped and stop == 95.0


def t_uses_stricter_of_last_and_mark_short():
    # mark > last: short floor uses mark
    stop, clamped, _ = B.clamp_sl_trigger(-1, 100.5, last_px=100.0, mark_px=101.0, buffer_pct=0.001)
    assert clamped and abs(stop - 101.0 * 1.001) < 1e-9


def t_uses_stricter_of_last_and_mark_long():
    # last < mark: long ceiling uses last (min)
    stop, clamped, _ = B.clamp_sl_trigger(1, 100.5, last_px=99.0, mark_px=100.0, buffer_pct=0.001)
    assert clamped and abs(stop - 99.0 * 0.999) < 1e-9


def t_live_sl_refs_from_position():
    last, mark = B.live_sl_refs({"last": "160.44", "markPx": "160.36"})
    assert last == 160.44 and mark == 160.36
    assert B.live_sl_refs({}) == (None, None)


class FakeAlgoX:
    def __init__(self):
        self.amends, self.places, self.algos = [], [], [
            {"algoId": "A1", "slTriggerPx": "167.43", "sz": "521.3"}
        ]
    def pending_algos(self, inst_id=None, inst_type=None):
        return list(self.algos)
    def amend_algo(self, body):
        self.amends.append(body)
        # simulate OKX reject if SL <= last
        px = float(body["newSlTriggerPx"])
        if px <= 160.44:
            raise B.OKXError("POST /api/v5/trade/amend-algos -> sCode=51278 SL trigger price cannot be lower than the last price")
        self.algos = [{"algoId": "A1", "slTriggerPx": body["newSlTriggerPx"], "sz": "521.3"}]
        return [{}]
    def place_algo(self, body):
        self.places.append(body)
        px = float(body["slTriggerPx"])
        if px <= 160.36:  # mark rule
            raise B.OKXError("place sCode=51302 SL trigger price cannot be lower than the mark price")
        self.algos = [{"algoId": "A2", "slTriggerPx": body["slTriggerPx"], "sz": body["sz"]}]
        return [{}]
    def cancel_algos(self, items):
        pass


def _bot_for_replace():
    b = B.Bot.__new__(B.Bot)
    b.log = {"events": [], "errors": []}
    b.ev = lambda msg, **k: b.log["events"].append({"msg": msg, **k})
    b.err = lambda m: b.log["errors"].append(m)
    b.x = FakeAlgoX()
    b.instr = {"AAVE-USDT-SWAP": {"tickSz": "0.01", "lotSz": "0.1"}}
    b.sl_algos = lambda inst: b.x.pending_algos(inst)
    b.place_protective_sl = lambda *a, **k: b.x.place_algo({
        "slTriggerPx": B.fmt_px(a[3], "0.01"), "sz": str(a[2]), "slTriggerPxType": "mark"})
    # speed up confirm loop
    import bot as botmod
    botmod.time.sleep = lambda s: None
    return b


def t_replace_sl_skips_marketable_without_error():
    b = _bot_for_replace()
    algo = b.x.algos[0]
    # Proposed trail below live mark — clamp makes it tighter than 167.43 and valid
    ok, _eff = b.replace_sl("AAVE-USDT-SWAP", -1, 521.3, algo, 159.85, "short", "0.01",
                      last_px=160.44, mark_px=160.36)
    assert ok, b.log
    assert not b.log["errors"], b.log["errors"]
    assert b.x.amends and float(b.x.amends[0]["newSlTriggerPx"]) > 160.44
    assert float(b.x.algos[0]["slTriggerPx"]) > 160.44


def t_replace_sl_skip_when_clamp_not_tighter():
    b = _bot_for_replace()
    # Exchange stop already tight just above mark; proposed even lower -> clamp equals ~floor, not tighter
    b.x.algos = [{"algoId": "A1", "slTriggerPx": "160.6", "sz": "521.3"}]
    algo = b.x.algos[0]
    ok, _eff = b.replace_sl("AAVE-USDT-SWAP", -1, 521.3, algo, 159.0, "short", "0.01",
                      last_px=160.44, mark_px=160.36)
    assert ok is False
    assert not b.x.amends and not b.log["errors"]
    assert any("stop amend skipped" in e["msg"] for e in b.log["events"])


def t_liq_nudge_rejects_marketable_vs_live():
    b = _bot_for_replace()
    b.x.current_levels_called = False
    # stub liqmap path: force a candidate below live mark
    import liqmap
    orig = liqmap.current_levels
    liqmap.current_levels = lambda *a, **k: ([], None)
    # adjust_stop won't find clusters with empty levels -> None; instead monkeypatch adjust_stop
    orig_adj = liqmap.adjust_stop
    liqmap.adjust_stop = lambda side, price, stop, atr, levels, **k: (
        159.5, {"clusters": [(159.0, 159.4, 1000)], "old_stop": stop, "new_stop": 159.5, "dist_pct": 1.0})
    try:
        rec = {"entry": 160.31, "init_stop": 166.24}
        out = b.liq_nudge_open("AAVE-USDT-SWAP", -1, rec, 158.0, 160.36, 1.85,
                               last_px=160.44, mark_px=160.36)
        assert out is None
        assert any(e.get("reason") == "would trigger immediately vs live last/mark"
                   for e in b.log.get("stop_adjustments_rejected", []))
    finally:
        liqmap.current_levels = orig
        liqmap.adjust_stop = orig_adj


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("t_"):
            f(); n += 1; print("PASS", k)
    print(f"{n} tests passed")
