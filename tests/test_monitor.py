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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
