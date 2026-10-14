"""Snapshot regression tests: fake market/private client and feeds, no network."""
import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from contextlib import redirect_stdout
from unittest.mock import patch

os.environ.setdefault("OKXBOT_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from okxbot import config, snapshot as S, news, events
from scripts.snapshot import main

NOW = dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)
SWAP, SPOT = "BTC-USDT-SWAP", "BTC-USDT"


class Fake:
    has_keys = True

    def __init__(self):
        self.calls = []

    def instruments(self, kind="SWAP", demo=False):
        return [{"instId": SWAP if kind == "SWAP" else SPOT, "state": "live", "instCategory": "1",
                 "settleCcy": "USDT", "ctType": "linear", "quoteCcy": "USDT", "baseCcy": "BTC",
                 "tickSz": "0.1", "lotSz": "0.01", "minSz": "0.01", "ctVal": "2" if kind == "SWAP" else "",
                 "lever": "5"}]

    def tickers(self, kind="SWAP"):
        return [{"instId": SWAP if kind == "SWAP" else SPOT, "last": "100", "volCcy24h": "100000000"}]

    def mark_prices(self, kind="SWAP", demo=False):
        return [{"instId": SWAP, "markPx": "101"}]

    def recent_candles(self, inst, bar, count):
        step = S.BAR_MS[bar]
        end = int(NOW.timestamp() * 1000) // step * step
        return [[str(end - i * step), "100", "102", "98", str(100 + (i % 5) / 10), "20", "20", "2000", "1" if i else "0"]
                for i in range(count - 1, -1, -1)]

    def balance(self):
        return [{"totalEq": "1000", "details": [{"ccy": "USDT", "eq": "800", "availEq": "700", "availBal": "600"},
                                                   {"ccy": "BTC", "eq": "2", "cashBal": "2", "availBal": "1", "accAvgPx": "90"}]}]

    def positions(self):
        return [{"instId": SWAP, "pos": "-2", "posSide": "net", "avgPx": "99", "last": "100", "markPx": "101", "lever": "3"}]

    def pending_algos(self, inst_type=None):
        return [{"instId": SWAP if inst_type == "SWAP" else SPOT, "algoId": inst_type,
                 "side": "buy" if inst_type == "SWAP" else "sell", "slTriggerPx": "105", "tpTriggerPx": "90"}]

    def _req(self, method, path, params=None, **kwargs):
        assert method == "GET", "snapshot attempted a mutation"
        self.calls.append((path, params, kwargs))
        if kwargs.get("private"):
            assert self.has_keys
            assert (method, path) in config.PRIVATE_WHITELIST
        if path.endswith("trade-fee"):
            return [{"maker": "-0.001", "taker": "-0.002", "makerU": "0.0001", "takerU": "-0.0005"}]
        if path.endswith("orders-pending"):
            return [{"instId": SPOT, "ordId": "pending"}]
        if path.endswith("funding-rate"):
            return [{"fundingRate": "0.001", "fundingTime": "1791979200000", "prevFundingTime": "1791964800000"}]
        if path.endswith("index-tickers"):
            return [{"idxPx": "100.5"}]
        if path.endswith("books"):
            return [{"bids": [["99.9", "10000"]], "asks": [["100.1", "10000"]]}]
        if path.endswith("open-interest-history"):
            return [["1791979200000", "0", "1000"]]
        raise AssertionError(path)


def feeds(max_age_h=24):
    def item(title, url, hours=1):
        return {"title": title, "url": url, "src": "fake", "summary": "<b>Bitcoin</b> " + "x" * 550,
                "published_at": S.iso(NOW - dt.timedelta(hours=hours))}
    return [item("Bitcoin rallies!", "https://EXAMPLE.com/a/?utm_source=rss#foo"),
            item("Bitcoin rallies", "https://example.com/b", 2),
            item("Other headline", "https://example.com/a", 3),
            item("Ethereum update", "https://example.com/c", 4),
            item("Old", "https://example.com/old", 25), item("Future", "https://example.com/new", -1)], []


def build(client=None, **kwargs):
    with patch.object(S, "fetch_events", return_value=[]):
        return S.build_snapshot(client or Fake(), inst=[SWAP, SPOT], now=NOW, state={}, news_fetcher=feeds, **kwargs)


def t_candles_indicators_and_reference():
    data = build()
    assert all(data["completeness"].values()), data["errors"]
    for bar, count in S.BAR_COUNTS.items():
        candles = data["instruments"][SWAP]["candles"][bar]
        assert len(candles["bars"]) == count
        assert all(row[0] + S.BAR_MS[bar] <= NOW.timestamp() * 1000 for row in candles["bars"])
        assert all(len(row) == 6 for row in candles["bars"])
        assert set(candles["indicators"]) == {"ema20", "ema50", "ema200", "atr14", "rsi14", "ret_24h", "ret_7d"}
        assert all(v is not None for v in candles["indicators"].values())
    assert data["instruments"][SWAP]["c7"]["signal"] in (-1, 0, 1)
    assert "reference only" in data["instruments"][SWAP]["c7"]["reason"]
    assert "insufficient" not in data["instruments"][SWAP]["c7"]["reason"]
    fake = Fake()
    original = fake.recent_candles
    fake.recent_candles = lambda *a: original(*a)[-11:]
    candles, frame = S.fetch_candles(fake, SWAP, "1H", NOW)
    assert candles["indicators"]["ema20"] is None
    assert candles["indicators"]["ema200"] is None
    assert S.c7_reference(frame)["signal"] == 0


def t_news_dedup_and_no_scoring():
    with patch.object(news, "_score", side_effect=AssertionError("sentiment called")), patch.object(news, "sentiment", side_effect=AssertionError()):
        data = build()
    assert len(data["news"]) == 2
    assert data["news"][0]["url"] == "https://example.com/a"
    assert data["news"][0]["id"] == hashlib.sha1(b"https://example.com/a").hexdigest()
    assert data["news"][0]["tags"] == ["BTC"]
    assert all(len(i["summary"]) <= 500 and "<b>" not in i["summary"] for i in data["news"])
    assert "sentiment" not in json.dumps(data, sort_keys=True, ensure_ascii=False).lower()


def t_account_fees_depth_and_state_readonly():
    fake = Fake()
    state = {"positions": {SWAP: {"stop": 105}}, "spot": {"longs": {SPOT: {"entry": 90}}},
             "day": "2026-10-14", "day_start_equity": 1200, "peak_equity": 1500}
    before = copy.deepcopy(state)
    with patch.object(S, "fetch_events", return_value=[]):
        data = S.build_snapshot(fake, [SWAP, SPOT], now=NOW, state=state, no_news=True)
    assert state == before
    assert data["guardrails"]["must_flatten"]
    assert data["account"]["avail_usdt"] == 700
    swap, spot = data["account"]["positions"]
    assert swap["side"] == "short" and swap["size"] == 2 and swap["sl"]["algoId"] == "SWAP"
    assert spot["last"] == 100 and spot["state_record"] == {"entry": 90}
    info = data["instruments"][SWAP]
    assert info["fees"] == {"maker": -0.0001, "taker": 0.0005, "source": "account"}
    assert info["funding"]["rate_8h"] == 0.002
    assert info["orderbook"]["depth_usdt_within_0.5pct_bid"] == 99.9 * 10000 * 2
    assert info["liquidity_ok"] is True
    assert config.DEMO is True


def t_hash_and_atomic_files_and_view():
    data = build()
    assert data == build()
    assert S.finalize(data) == data
    modified = copy.deepcopy(data)
    modified["demo"] = False
    assert S.finalize(modified)["sha256"] != data["sha256"]
    with tempfile.TemporaryDirectory() as directory, patch.object(config, "DATA_DIR", directory):
        S.write_snapshot(data, Path(directory, "extra.json"))
        for name in ("snapshots/latest.json", "snapshots/" + data["snapshot_id"] + ".json", "extra.json"):
            assert json.loads(Path(directory, name).read_text()) == data
        assert len(list(Path(directory, "snapshots").iterdir())) == 2
    view = S.render_view(data)
    assert "POSITION" in view and "NEWS" in view and "BTC-USDT" in view
    assert view.splitlines()[-1] == data["snapshot_id"]


def t_dry_cli_and_failures_exit_zero():
    fake = Fake()
    fake.has_keys = False
    with tempfile.TemporaryDirectory() as directory, patch.object(config, "DATA_DIR", directory), patch.object(S, "fetch_events", return_value=[]):
        for flags in ([], ["--view"]):
            output = io.StringIO()
            with redirect_stdout(output):
                assert main(["--inst", SWAP, "--no-news", *flags], client=fake) == 0
            data = json.loads(Path(directory, "snapshots/latest.json").read_text())
            assert data["account"] is None and not data["completeness"]["account"]
            assert not data["account_available"] and not data["completeness"]["news"]
            assert data["instruments"][SWAP]["fees"]["source"] == "config"
            if not flags:
                assert json.loads(output.getvalue()) == data
    assert not any(kw.get("private") for _, _, kw in fake.calls)
    fake.recent_candles = lambda *a: (_ for _ in ()).throw(ValueError("candles unavailable"))
    data = build(fake)
    assert not data["completeness"]["market"] and data["completeness"]["news"]
    assert data["instruments"][SWAP]["last"] == 100
    assert any("candles unavailable" in e for e in data["errors"])
    json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False)


def t_events_week_and_default_universe():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory, "events.json")
        path.write_text(json.dumps({"events": [{"name": "CPI", "time_et": "2026-10-15T08:30:00", "source": "https://www.bls.gov/"},
                                                {"name": "late", "time_et": "2026-11-15T08:30:00", "source": "https://www.bls.gov/"}]}))
        with patch.object(events, "EVENTS_F", str(path)):
            result = S.fetch_events(NOW)
        assert len(result) == 1 and result[0]["source"] == "https://www.bls.gov/"
        json.dumps(result, sort_keys=True)
    from okxbot import tradfi
    with patch.object(tradfi, "eligible", return_value=([], [], {})):
        errors = []
        universe = S.default_universe(S.CachedClient(Fake()), {"positions": {"HELD-USDT-SWAP": {}}}, None, errors)
    assert not errors, errors
    assert {SWAP, SPOT, "HELD-USDT-SWAP", "QQQ-USDT-SWAP"} <= set(universe)


def t_feed_fetch_preserves_metadata():
    xml = b'<rss><channel><item><title>Bitcoin news</title><link>https://example.com/story</link><pubDate>Wed, 14 Oct 2026 11:00:00 GMT</pubDate><description>&lt;b&gt;Description&lt;/b&gt;</description></item></channel></rss>'
    class Response:
        content = xml
        def raise_for_status(self):
            pass
    with patch.object(news.requests, "get", return_value=Response()), patch.object(news, "FEEDS", ["https://example.com/rss"]):
        items, errors = news.fetch_items(max_age_h=100000)
    assert not errors and items[0]["url"] == "https://example.com/story"
    assert items[0]["published_at"].startswith("2026-10-14T11:00:00")
    assert "Description" in items[0]["summary"]


def t_partial_private_failures_and_cache():
    fake = Fake()
    original = fake._req
    def failing(method, path, *args, **kwargs):
        if path.endswith("trade-fee") or path.endswith("orders-pending"):
            raise RuntimeError("endpoint unavailable")
        return original(method, path, *args, **kwargs)
    fake._req = failing
    data = build(fake)
    assert data["account"] is not None and not data["account_available"]
    assert data["account"]["pending_orders"] is None
    assert data["instruments"][SWAP]["fees"]["source"] == "config"
    assert not data["completeness"]["market"]
    assert any("fees" in e for e in data["errors"])
    cache = S.CachedClient(Fake())
    first = cache._req("GET", "/api/v5/market/books", {"instId": SWAP})
    first[0]["bids"] = []
    assert cache._req("GET", "/api/v5/market/books", {"instId": SWAP})[0]["bids"]
    assert len(cache.client.calls) == 1
    try:
        cache._req("POST", "/api/v5/trade/order", {})
        assert False, "POST accepted"
    except ValueError:
        pass


def t_skipped_news_missing_events_and_atomic_failure():
    with patch.object(news, "fetch_items", side_effect=AssertionError("news should be skipped")), patch.object(events, "EVENTS_F", "/nonexistent/calendar"):
        data = S.build_snapshot(Fake(), inst=[], no_news=True, now=NOW, state={})
    assert not data["completeness"]["news"] and not data["completeness"]["events"]
    assert any("calendar unavailable" in e for e in data["errors"])
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory, "latest.json")
        path.write_text('{"old": true}')
        with patch.object(S.os, "replace", side_effect=OSError("replace failed")):
            try:
                S.atomic_json(path, data)
                assert False
            except OSError:
                pass
        assert json.loads(path.read_text()) == {"old": True}
        assert list(Path(directory).iterdir()) == [path]


def t_hash_matches_canonical_content():
    data = build()
    payload = {k: v for k, v in data.items() if k not in ("snapshot_id", "sha256")}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                      separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    assert data["sha256"] == digest
    assert data["snapshot_id"] == "snap-20261014T120000Z-" + digest[:10]


if __name__ == "__main__":
    # Fail immediately if any test leaks to the network.
    with patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")):
        for name, func in list(globals().items()):
            if name.startswith("t_"):
                func()
                print("PASS", name)
