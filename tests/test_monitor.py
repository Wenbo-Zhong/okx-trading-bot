"""Monitor tests: alt selection filters, US-hours gating, hysteresis, wake caps. Offline."""
import datetime as dt, os, sys, tempfile
from pathlib import Path
os.environ.setdefault("OKXBOT_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from okxbot.monitor import settings, signals, watchlist
from okxbot.monitor.wake import Waker

CFG = settings.load()
UTC = dt.timezone.utc


def t(inst, bid=100.0, ask=100.02):
    return {"instId": inst, "bidPx": str(bid), "askPx": str(ask), "last": "100", "volCcy24h": "1000"}


def test_select_filters():
    pool = [t("AAA-USDT-SWAP"), t("ZRO-USDT-SWAP"), t("HELD-USDT-SWAP"), t("FLAG-USDT-SWAP"),
            t("WIDE-USDT-SWAP", 100, 100.1), t("CALM-USDT-SWAP"), t("WILD-USDT-SWAP"), t("THIN-USDT-SWAP"),
            t("USER-USDT-SWAP"), t("BBB-USDT-SWAP")]
    vols = {"CALM-USDT-SWAP": 1.0, "WILD-USDT-SWAP": 30.0}
    cfg = dict(CFG, alt_count=15, user_exclude=["USER"])
    exclude = {"ZRO", "HELD"} | set(cfg["user_exclude"])
    picked, rej = watchlist.select_alts(pool, cfg, exclude, {"FLAG-USDT-SWAP"},
                                        lambda i: vols.get(i, 6.0),
                                        lambda i: 10 if i.startswith("THIN") else 1e6)
    assert picked == ["AAA-USDT-SWAP", "BBB-USDT-SWAP"], picked
    assert rej["ZRO-USDT-SWAP"] == "excluded" and rej["HELD-USDT-SWAP"] == "excluded"
    assert rej["USER-USDT-SWAP"] == "excluded"
    assert rej["FLAG-USDT-SWAP"].startswith("flagged")
    assert rej["WIDE-USDT-SWAP"].startswith("spread")
    assert rej["CALM-USDT-SWAP"].startswith("7d vol") and rej["WILD-USDT-SWAP"].startswith("7d vol")
    assert rej["THIN-USDT-SWAP"].startswith("thin")
    picked, _ = watchlist.select_alts(pool, dict(cfg, alt_count=1), set(), set(), lambda i: 6.0)
    assert picked == ["AAA-USDT-SWAP"]


def test_volume_pool_and_stocks():
    tk = [dict(t("A-USDT-SWAP"), volCcy24h="5"), dict(t("B-USDT-SWAP"), volCcy24h="50"),
          dict(t("NVDA-USDT-SWAP"), volCcy24h="9"), dict(t("TSLA-USDT-SWAP"), volCcy24h="99"),
          t("QQQ-USDT-SWAP"), t("SPY-USDT-SWAP"), t("A-USDT")]
    pool = watchlist.volume_pool(tk, {"A-USDT-SWAP", "B-USDT-SWAP"}, 1)
    assert [p["instId"] for p in pool] == ["B-USDT-SWAP"]
    avail = {x["instId"] for x in tk}
    s = watchlist.pick_stocks(tk, dict(CFG, stock_extra=1), avail)
    assert s == ["QQQ-USDT-SWAP", "SPY-USDT-SWAP", "TSLA-USDT-SWAP"], s
    assert abs(watchlist.daily_vol_pct([100, 101, 100, 101, 100]) - 1.15) < 0.05


def test_rotation_monday():
    sun = dt.datetime(2026, 10, 18, 15, 0, tzinfo=UTC)   # Sun 23:00 Shanghai
    mon = dt.datetime(2026, 10, 18, 16, 30, tzinfo=UTC)  # Mon 00:30 Shanghai
    assert watchlist.rotation_key(sun) != watchlist.rotation_key(mon)


def test_us_hours():
    assert signals.us_market_open(dt.datetime(2026, 10, 15, 14, 0, tzinfo=UTC))      # Thu 10:00 ET
    assert not signals.us_market_open(dt.datetime(2026, 10, 15, 13, 0, tzinfo=UTC))  # 09:00 ET
    assert not signals.us_market_open(dt.datetime(2026, 10, 15, 20, 30, tzinfo=UTC)) # 16:30 ET
    assert not signals.us_market_open(dt.datetime(2026, 10, 17, 15, 0, tzinfo=UTC))  # Saturday


def bars(n=120, spike=False):
    out = [{"ts": i, "o": 100, "h": 100.5, "l": 99.5, "c": 100 + (0.01 if i % 2 else -0.01), "v": 10} for i in range(n)]
    if spike:
        out[-1] = {"ts": n - 1, "o": 100, "h": 104, "l": 100, "c": 103.5, "v": 100}
    return out


def test_anomaly_hysteresis_and_stock_gating():
    latch = signals.Armed()
    open_t = dt.datetime(2026, 10, 15, 14, 0, tzinfo=UTC)
    closed_t = dt.datetime(2026, 10, 15, 22, 0, tzinfo=UTC)
    ev = signals.evaluate("X", "1H", bars(spike=True), "alt", CFG, latch)
    assert [e["kind"] for e in ev] == ["price_volume_anomaly"]
    assert signals.evaluate("X", "1H", bars(spike=True), "alt", CFG, latch) == []      # latched
    signals.evaluate("X", "1H", bars(), "alt", CFG, latch)                              # calm -> re-arm
    assert signals.evaluate("X", "1H", bars(spike=True), "alt", CFG, latch)
    assert signals.evaluate("Q", "1H", bars(spike=True), "stock", CFG, signals.Armed(), now=closed_t) == []
    kinds = {e["kind"] for e in signals.evaluate("Q", "1H", bars(spike=True), "stock", CFG, signals.Armed(), now=open_t)}
    assert "price_volume_anomaly" in kinds and "breakout_up" in kinds
    # price-only move (no volume) is not an anomaly for alts
    b = bars(spike=True); b[-1]["v"] = 10
    assert signals.evaluate("Y", "1H", b, "alt", CFG, signals.Armed()) == []


def test_position_checks():
    latch, breach = signals.Armed(), {}
    pos = {"side": 1, "entry": 100, "init_stop": 95, "stop": 95, "tp": 110}
    assert signals.position_checks("P", pos, 101, 1.0, CFG, latch, breach, 0) == []
    kinds = {e["kind"] for e in signals.position_checks("P", pos, 95.2, 1.0, CFG, latch, breach, 1)}
    assert kinds == {"near_stop", "adverse_move"}
    assert signals.position_checks("P", pos, 95.1, 1.0, CFG, latch, breach, 2) == []
    assert signals.position_checks("P", pos, 94.9, 1.0, CFG, latch, breach, 3) == []
    ev = signals.position_checks("P", pos, 94.8, 1.0, CFG, latch, breach, 3 + CFG["stop_breach_grace_s"])
    assert ev and ev[0]["kind"] == "stop_not_triggered" and ev[0]["severe"]


def test_wake_caps():
    now = [dt.datetime(2026, 10, 15, 2, 0, tzinfo=UTC).timestamp()]  # 10:00 Shanghai
    d = tempfile.mkdtemp()
    os.environ["OKX_WAKE_WEBHOOK_URL"] = "http://example.invalid"
    sent = []
    w = Waker(CFG, enabled=True, clock=lambda: now[0], post=lambda p: sent.append(p) or True,
              log_path=os.path.join(d, "l"), state_path=os.path.join(d, "s"))
    w.add({"kind": "a", "inst": "X"}); w.add({"kind": "b", "inst": "Y"}); w.add({"kind": "a", "inst": "X"})
    assert w.tick() is None                                   # coalesce window
    now[0] += CFG["coalesce_s"]
    p = w.tick(); assert p and p["count"] == 2 and len(sent) == 1
    now[0] += 60; w.add({"kind": "c", "inst": "Z"}); now[0] += CFG["coalesce_s"]
    assert w.tick() is None                                   # hourly cap
    w.add({"kind": "fault", "severe": True, "dedupe": "f1"})
    p = w.tick(); assert p and p["severe"] and p["count"] == 2  # severe bypasses cap, carries pending
    now[0] += 3600; w.add({"kind": "d"}); now[0] += CFG["coalesce_s"]
    assert w.tick()
    w.st["count"] = CFG["wake_max_per_day"]; now[0] += 4000; w.add({"kind": "e"}); now[0] += 100
    assert w.tick() is None                                   # daily cap
    off = Waker(CFG, enabled=False, clock=lambda: now[0], post=lambda p: sent.append(p),
                log_path=os.path.join(d, "l2"), state_path=os.path.join(d, "s2"))
    off.add({"kind": "z", "severe": True}); n = len(sent); off.tick()
    assert len(sent) == n and not off.enabled                 # log-only never posts
    del os.environ["OKX_WAKE_WEBHOOK_URL"]



def trend_flip_bars(vol_last=10, body_extra=0.0):
    """Uptrend then downtrend that crosses EMA on the last bar; optional fat last candle."""
    out, c = [], 100.0
    for i in range(80):
        c += 0.2
        out.append({"ts": i, "o": c - 0.05, "h": c + 0.15, "l": c - 0.15, "c": c, "v": 10})
    for i in range(80, 101):
        c -= 0.35
        o, h, l, cl, v = c + 0.05, c + 0.15, c - 0.2, c, 10
        if i == 100:
            v = vol_last
            if body_extra:
                o = cl + body_extra
                h, l = max(h, o), min(l, cl)
        out.append({"ts": i, "o": o, "h": h, "l": l, "c": cl, "v": v})
    return out


def test_15m_trend_change_priority():
    """Weak 15m flips are priority=low (log only); strong ones and any 1H flip stay normal."""
    weak = trend_flip_bars(vol_last=10, body_extra=0.0)
    strong = trend_flip_bars(vol_last=40, body_extra=1.0)
    latch = signals.Armed()
    ev = signals.evaluate("OKB-USDT-SWAP", "15m", weak, "major", CFG, latch)
    tc = [e for e in ev if e["kind"] == "trend_change"]
    assert tc and tc[0]["priority"] == "low" and tc[0]["direction"] == -1
    assert tc[0]["detail"]["rel_vol"] < CFG["trend_change_15m_min_rel_vol"] or \
           tc[0]["detail"]["body_atr"] < CFG["trend_change_15m_min_body_atr"]
    latch = signals.Armed()
    ev = signals.evaluate("OKB-USDT-SWAP", "15m", strong, "major", CFG, latch)
    tc = [e for e in ev if e["kind"] == "trend_change"]
    assert tc and tc[0]["priority"] == "normal"
    assert tc[0]["detail"]["rel_vol"] >= CFG["trend_change_15m_min_rel_vol"]
    assert tc[0]["detail"]["body_atr"] >= CFG["trend_change_15m_min_body_atr"]
    # 1H is never downgraded by the 15m filter
    latch = signals.Armed()
    ev = signals.evaluate("OKB-USDT-SWAP", "1H", weak, "major", CFG, latch)
    tc = [e for e in ev if e["kind"] == "trend_change"]
    assert tc and tc[0]["priority"] == "normal"


def test_kind_cooldown_and_low_priority_no_wake():
    """Same kind+inst+bar cannot wake within kind_cooldown_s unless direction flips with high rel_vol.
    priority=low events are logged but never enter the pending wake batch."""
    now = [dt.datetime(2026, 10, 15, 2, 0, tzinfo=UTC).timestamp()]
    d = tempfile.mkdtemp()
    w = Waker(CFG, enabled=False, clock=lambda: now[0],
              log_path=os.path.join(d, "l"), state_path=os.path.join(d, "s"))
    # low-priority: logged, not pending
    assert w.add({"kind": "trend_change", "inst": "OKB-USDT-SWAP", "bar": "15m", "priority": "low",
                  "direction": -1, "bar_ts": 1, "detail": {"rel_vol": 0.25, "body_atr": 0.27, "trend": -1}}) is False
    assert w.pending == []
    # first normal wake of this kind
    assert w.add({"kind": "trend_change", "inst": "OKB-USDT-SWAP", "bar": "15m", "priority": "normal",
                  "direction": -1, "bar_ts": 2, "detail": {"rel_vol": 2.0, "body_atr": 1.2, "trend": -1}})
    now[0] += CFG["coalesce_s"]
    assert w.tick() and w.st["kind_last"]["trend_change:OKB-USDT-SWAP:15m"]["direction"] == -1
    # same direction within 2h -> suppressed
    now[0] += 60
    assert w.add({"kind": "trend_change", "inst": "OKB-USDT-SWAP", "bar": "15m", "priority": "normal",
                  "direction": -1, "bar_ts": 3, "detail": {"rel_vol": 2.0, "body_atr": 1.0, "trend": -1}}) is False
    assert w.pending == []
    # opposite direction but weak rel_vol -> still suppressed
    assert w.add({"kind": "trend_change", "inst": "OKB-USDT-SWAP", "bar": "15m", "priority": "normal",
                  "direction": 1, "bar_ts": 4, "detail": {"rel_vol": 1.0, "body_atr": 1.0, "trend": 1}}) is False
    # opposite direction with rel_vol >= flip threshold -> allowed
    assert w.add({"kind": "trend_change", "inst": "OKB-USDT-SWAP", "bar": "15m", "priority": "normal",
                  "direction": 1, "bar_ts": 5, "detail": {"rel_vol": CFG["kind_cooldown_flip_rel_vol"],
                                                          "body_atr": 1.0, "trend": 1}})
    # different instrument is independent
    assert w.add({"kind": "trend_change", "inst": "BTC-USDT-SWAP", "bar": "15m", "priority": "normal",
                  "direction": -1, "bar_ts": 6, "detail": {"rel_vol": 2.0, "trend": -1}})
    # flush pending (respect global wake_min_interval + coalesce)
    now[0] = max(now[0] + CFG["coalesce_s"], w.st["last_wake"] + CFG["wake_min_interval_s"] + CFG["coalesce_s"])
    assert w.tick()
    assert w.st["kind_last"]["trend_change:OKB-USDT-SWAP:15m"]["direction"] == 1
    # still inside kind cooldown -> suppressed
    now[0] += 60
    assert w.add({"kind": "trend_change", "inst": "OKB-USDT-SWAP", "bar": "15m", "priority": "normal",
                  "direction": 1, "bar_ts": 7, "detail": {"rel_vol": 1.2, "trend": 1}}) is False
    # after kind cooldown elapses, same direction allowed again
    now[0] += CFG["kind_cooldown_s"]
    assert w.add({"kind": "trend_change", "inst": "OKB-USDT-SWAP", "bar": "15m", "priority": "normal",
                  "direction": 1, "bar_ts": 8, "detail": {"rel_vol": 1.2, "trend": 1}})


def test_settings_override_from_config_json():
    """data/monitor_config.json overrides DEFAULTS for the new thresholds."""
    import json
    from okxbot.monitor import settings as ms
    d = tempfile.mkdtemp()
    path = os.path.join(d, "monitor_config.json")
    with open(path, "w") as f:
        json.dump({"trend_change_15m_min_rel_vol": 2.5, "kind_cooldown_s": 1800}, f)
    old = ms.PATH
    try:
        ms.PATH = path
        cfg = ms.load()
        assert cfg["trend_change_15m_min_rel_vol"] == 2.5
        assert cfg["kind_cooldown_s"] == 1800
        assert cfg["kind_cooldown_flip_rel_vol"] == ms.DEFAULTS["kind_cooldown_flip_rel_vol"]
        assert cfg["majors"] == ms.DEFAULTS["majors"]
    finally:
        ms.PATH = old


def _w(cfg, now, d, name):
    return Waker(cfg, enabled=False, clock=lambda: now[0], log_path=os.path.join(d, name + ".l"),
                 state_path=os.path.join(d, name + ".s"))


def test_wake_budget_shared_weekly_and_persisted():
    """Codex #5: every wake type shares a persisted weekly budget; faults keep a small reserve."""
    cfg = dict(CFG, wake_max_per_day=50, wake_max_per_week=5, wake_fault_reserve_week=2, wake_fault_reserve_day=2,
               wake_min_interval_s=0, coalesce_s=0, fault_cooldown_s=0, kind_cooldown_s=0)
    now = [dt.datetime(2026, 10, 19, 2, 0, tzinfo=UTC).timestamp()]   # Monday 10:00 Shanghai
    d = tempfile.mkdtemp()
    w = _w(cfg, now, d, "a")
    kinds = ["breakout_up", "material_news", "mm_glitch_confirmed", "adverse_move"]
    got = []
    for i, k in enumerate(kinds):
        w.add({"kind": k, "inst": f"I{i}", "bar_ts": i, "signal": "mm_glitch" if k.startswith("mm") else None})
        now[0] += 1; got.append(bool(w.tick()))
    assert got == [True, True, True, False], got        # ordinary wakes stop at week cap - fault reserve
    w2 = _w(cfg, now, d, "a")                            # restart: budget persisted
    assert w2.st["week_count"] == 3 and w2.st["week"] == "2026-W43"
    w2.add({"kind": "x", "inst": "Z", "bar_ts": 9}); now[0] += 1
    assert w2.tick() is None
    for i in range(3):                                   # faults use the reserve, then stop at the week cap
        w2.add({"kind": "protection_fault", "category": "fault", "severe": True, "dedupe": f"f{i}"})
        now[0] += 1; r = w2.tick()
        assert bool(r) == (i < 2), i
    assert w2.st["week_count"] == 5 and w2.st["week_faults"] == 2
    now[0] += 7 * 86400                                  # next ISO week resets
    w2.add({"kind": "y", "inst": "Q", "bar_ts": 10}); now[0] += 1
    assert w2.tick() and w2.st["week_count"] == 1


def test_wake_daily_limit_zero_dispatches_nothing():
    cfg = dict(CFG, wake_max_per_day=0)
    now = [dt.datetime(2026, 10, 19, 2, 0, tzinfo=UTC).timestamp()]
    w = _w(cfg, now, tempfile.mkdtemp(), "z")
    for i, e in enumerate([{"kind": "breakout_up", "inst": "A"}, {"kind": "mm_glitch_update", "inst": "ETH",
                           "regime_wake": True, "signal": "mm_glitch"},
                           {"kind": "circuit_breaker_halted", "category": "fault", "severe": True}]):
        w.add(dict(e, bar_ts=i)); now[0] += 4000
        assert w.tick() is None
    assert w.st["count"] == 0


def test_fault_reserve_beyond_daily_cap():
    cfg = dict(CFG, wake_max_per_day=1, wake_fault_reserve_day=1, wake_max_per_week=30, coalesce_s=0,
               fault_cooldown_s=0)
    now = [dt.datetime(2026, 10, 19, 2, 0, tzinfo=UTC).timestamp()]
    w = _w(cfg, now, tempfile.mkdtemp(), "f")
    w.add({"kind": "a", "inst": "A", "bar_ts": 1}); now[0] += 1; assert w.tick()
    w.add({"kind": "b", "inst": "B", "bar_ts": 2}); now[0] += 4000; assert w.tick() is None   # daily cap
    w.add({"kind": "halt", "severe": True, "dedupe": "h1"}); now[0] += 1
    p = w.tick(); assert p and p["fault"] and p["count"] == 2      # reserve carries the pending ordinary event
    w.add({"kind": "halt", "severe": True, "dedupe": "h2"}); now[0] += 1
    assert w.tick() is None                                         # reserve used up


def test_material_change_needed_within_cooldown():
    cfg = dict(CFG, coalesce_s=0, wake_min_interval_s=0)
    now = [dt.datetime(2026, 10, 19, 2, 0, tzinfo=UTC).timestamp()]
    w = _w(cfg, now, tempfile.mkdtemp(), "m")
    e = {"kind": "mm_glitch_update", "inst": "ETH", "bar": "1m", "signal": "mm_glitch"}
    assert w.add(dict(e, bar_ts=1, detail={"band": [100, 110]})); now[0] += 1; assert w.tick()
    assert w.add(dict(e, bar_ts=2, detail={"band": [100, 111]})) is False        # 10% move: not material
    assert w.add(dict(e, bar_ts=3, detail={"band": [97, 110]}))                  # 30% move: range changed
    assert w.pending[-1]["material_change"] == "band_change"
    r = {"kind": "adverse_move", "inst": "X", "bar": ""}
    now[0] += 1; w.tick()
    assert w.add(dict(r, bar_ts=4, detail={"risk_level": 1})); now[0] += 1; w.tick()
    assert w.add(dict(r, bar_ts=5, detail={"risk_level": 1})) is False
    assert w.add(dict(r, bar_ts=6, detail={"risk_level": 2}))                    # risk changed


def test_stale_pending_dropped():
    cfg = dict(CFG, pending_max_age_s=600, wake_max_per_day=1)
    now = [dt.datetime(2026, 10, 19, 2, 0, tzinfo=UTC).timestamp()]
    w = _w(cfg, now, tempfile.mkdtemp(), "s")
    w.add({"kind": "a", "inst": "A", "bar_ts": 1}); now[0] += CFG["coalesce_s"]; assert w.tick()
    w.add({"kind": "b", "inst": "B", "bar_ts": 2}); now[0] += 700
    assert w.tick() is None and w.pending == []


def test_news_confirmation_needs_same_entity_and_event():
    """Codex #4 (small fix): sharing two long words ('bitcoin', 'price') no longer counts as confirmation."""
    from okxbot import news
    assert not news.same_story("Bitcoin price slides as ETF outflows continue",
                               "Bitcoin price slides further, analysts warn")          # no shared HI event
    assert not news.same_story("Bybit hacked for $1.4 billion", "Upbit hacked, $30M stolen")   # different venue
    assert not news.same_story("Binance halts withdrawals amid outage", "Coinbase halts withdrawals for maintenance")
    assert news.same_story("Bybit hacked for $1.4 billion in ETH", "Hackers drain Bybit: exchange hacked, $1.4B stolen")
    assert news.same_story("Binance halts withdrawals amid outage", "Binance suspends withdrawals temporarily")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
