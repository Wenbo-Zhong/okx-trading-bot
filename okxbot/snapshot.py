"""Read-only planner inputs. No trading cycle, sentiment scoring or state writes.

Prices/books are production data. liquidity_ok is a reference check in both
 directions at START_CAPITAL * MAX_MARGIN_PCT, with a MIN_STOP_PCT stop;
it is not approval for any particular plan or for demo execution liquidity.
Canonical hashes use UTF-8 JSON, sorted keys, no whitespace and no NaN.
"""
import copy
import datetime as dt
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from okxbot import config, events, indicators, liqmap, news, sizing, strategy
from okxbot.risk import guardrail_status

UTC = dt.timezone.utc
BAR_COUNTS = {"15m": 48, "1H": 48, "4H": 60, "1D": 30}
BAR_MS = {"15m": 900_000, "1H": 3_600_000, "4H": 14_400_000, "1D": 86_400_000}


def number(value):
    if value is None or value == "":
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def attempt(errors, label, func, default=None):
    try:
        return func()
    except Exception as exc:
        errors.append(f"{label}: {type(exc).__name__}: {exc}")
        return default


class CachedClient:
    """Per-snapshot read cache, including universe requests; never caches writes."""
    def __init__(self, client):
        self.client, self.cache = client, {}

    def __getattr__(self, name):
        attr = getattr(self.client, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            if name == "_req" and args[0] != "GET":
                raise ValueError("snapshot only allows GET")
            key = (name, json.dumps([args, kwargs], sort_keys=True))
            if key not in self.cache:
                self.cache[key] = attr(*args, **kwargs)
            return copy.deepcopy(self.cache[key])
        return call


def fetch_candles(client, inst_id, bar, now):
    # Enough warmup for EMA200 and seven-day returns even on 15m bars.
    count = max(720 if bar == "1H" else 301, 7 * 86_400_000 // BAR_MS[bar] + 2)
    rows = client.recent_candles(inst_id, bar, count)
    closed = {int(r[0]): r for r in rows if str(r[8]) == "1"
              and int(r[0]) + BAR_MS[bar] <= now.timestamp() * 1000}
    rows = [closed[k] for k in sorted(closed)]
    if not rows:
        raise ValueError("no closed candles")
    frame = indicators.to_df(rows)
    enriched = indicators.add_indicators(frame)
    last = enriched.iloc[-1]
    values = {f"ema{n}": number(last[f"ema{n}"]) if len(frame) >= n else None
              for n in (20, 50, 200)}
    values.update(atr14=number(last["atr"]) if len(frame) >= 14 else None,
                  rsi14=number(last["rsi"]) if len(frame) >= 15 else None)
    for key, hours in (("ret_24h", 24), ("ret_7d", 168)):
        previous = frame[frame.ts == int(last.ts) - hours * 3_600_000]
        values[key] = (number(last.c / previous.iloc[-1].c - 1)
                       if len(previous) and previous.iloc[-1].c > 0 else None)
    bars = [[int(r[0]), *[float(v) for v in r[1:6]]] for r in rows[-BAR_COUNTS[bar]:]]
    return {"bars": bars, "indicators": values}, frame


def c7_reference(frame):
    result = {"signal": 0, "strength": None, "stop": None, "tp": None,
              "reason": "insufficient closed 1H history (need 300 bars); reference only"}
    if frame is None or len(frame) < 300:
        return result
    row = strategy.build_features(frame).iloc[-1]
    signal = int(row.signal)
    stop, tp = strategy.initial_stop_tp(signal, float(row.c), float(row.atr)) if signal else (None, None)
    result.update(signal=signal, strength=number(row.strength), stop=number(stop), tp=number(tp),
                  reason=("C7 reference only" if signal and stop is not None else
                          "C7 stop exceeds limit; reference only" if signal else "no C7 signal; reference only"))
    return result


def fetch_funding(client, inst_id):
    row = client._req("GET", "/api/v5/public/funding-rate", {"instId": inst_id})[0]
    current = int(row["fundingTime"])
    prev, nxt = int(row.get("prevFundingTime") or 0), int(row.get("nextFundingTime") or 0)
    hours = (current - prev) / 3.6e6 if prev else (nxt - current) / 3.6e6 if nxt else 8
    return {"rate_8h": float(row["fundingRate"]) * 8 / (hours if hours > 0 else 8),
            "next_time": iso(dt.datetime.fromtimestamp(current / 1000, UTC))}


def fetch_fees(client, inst_id, errors):
    swap = inst_id.endswith("-SWAP")
    fallback = {"maker": config.SWAP_MAKER_FEE if swap else config.SPOT_MAKER_FEE,
                "taker": config.SWAP_TAKER_FEE if swap else config.SPOT_FEE, "source": "config"}
    if not client.has_keys:
        return fallback

    def fetch():
        params = {"instType": "SWAP" if swap else "SPOT"}
        params["instFamily" if swap else "instId"] = inst_id.removesuffix("-SWAP") if swap else inst_id
        row = client._req("GET", "/api/v5/account/trade-fee", params, private=True)[0]
        # OKX negative rate means a charge; expose positive costs, negative rebates.
        maker = row.get("makerU", row.get("maker")) if swap else row.get("maker")
        taker = row.get("takerU", row.get("taker")) if swap else row.get("taker")
        return {"maker": -float(maker), "taker": -float(taker), "source": "account"}
    return attempt(errors, f"{inst_id} fees", fetch, fallback)


def fetch_book(client, inst_id, spec):
    book = client._req("GET", "/api/v5/market/books", {"instId": inst_id, "sz": "400"})[0]
    bid, ask = float(book["bids"][0][0]), float(book["asks"][0][0])
    mid = (bid + ask) / 2
    units = float(spec.get("ctVal") or 1) * float(spec.get("ctMult") or 1)
    result = {"bid": bid, "ask": ask, "spread_bps": (ask - bid) / mid * 10000}
    for side in ("bid", "ask"):
        result[f"depth_usdt_within_0.5pct_{side}"] = sizing.depth_within(book[side + "s"], mid, 0.005) * units
    floor = config.SLIP_FLOOR_MAJOR if inst_id.split("-")[0] in config.SLIP_MAJORS else config.SLIP_FLOOR_OTHER
    checks = [sizing.liquidity_size(book, side, mid, mid * (1 - side * config.MIN_STOP_PCT),
                                   config.START_CAPITAL * config.MAX_MARGIN_PCT, units, floor,
                                   config.LIQ_DEPTH_PCT, config.LIQ_MIN_COVER)[2] for side in (1, -1)]
    return result, all(checks)


def fetch_clusters(client, inst_id, frame):
    if frame is None:
        return None
    # Reuse liqmap's recent OI cache; all uncached I/O goes through the injected client.
    import time
    cached = liqmap._cache.get(inst_id)
    if cached and time.time() - cached[0] < 600:
        oi = cached[1]
    else:
        rows = client._req("GET", "/api/v5/rubik/stat/contracts/open-interest-history",
                           {"instId": inst_id, "period": "1H", "limit": 100})
        if not rows:
            return None
        oi = {int(r[0]): float(r[2]) for r in rows}
    bars = list(frame[["ts", "o", "h", "l", "c"]].itertuples(index=False, name=None))
    return [[float(v) for v in row] for row in liqmap.nearest_clusters(
        liqmap.build_levels(bars, oi), float(frame.iloc[-1].c))]


def fetch_instrument(client, inst_id, specs, ticks, marks, now, errors):
    market = "swap" if inst_id.endswith("-SWAP") else "spot"
    spec = specs.get(inst_id, {})
    if not spec:
        errors.append(f"{inst_id}: instrument spec unavailable")
    tick = ticks.get(inst_id, {})
    if not tick:
        errors.append(f"{inst_id}: ticker unavailable")
    out = {"market": market, "spec": {k: spec.get(k) for k in ("tickSz", "lotSz", "minSz", "ctVal")},
           "last": number(tick.get("last")), "mark": number(marks.get(inst_id, {}).get("markPx")),
           "index": None, "funding": None, "fees": fetch_fees(client, inst_id, errors),
           "orderbook": None, "candles": {}, "liq_clusters": None, "liquidity_ok": None}
    out["spec"]["maxLever"] = spec.get("lever") or spec.get("maxLever")
    if market == "swap":
        if out["mark"] is None:
            errors.append(f"{inst_id}: mark price unavailable")
        out["funding"] = attempt(errors, f"{inst_id} funding", lambda: fetch_funding(client, inst_id))
        out["index"] = attempt(errors, f"{inst_id} index", lambda: number(client._req(
            "GET", "/api/v5/market/index-tickers", {"instId": inst_id.removesuffix("-SWAP")})[0]["idxPx"]))
    book = attempt(errors, f"{inst_id} book", lambda: fetch_book(client, inst_id, spec))
    if book:
        out["orderbook"], out["liquidity_ok"] = book
    frames = {}
    for bar in BAR_COUNTS:
        result = attempt(errors, f"{inst_id} {bar}", lambda: fetch_candles(client, inst_id, bar, now))
        out["candles"][bar] = result[0] if result else None
        frames[bar] = result[1] if result else None
    out["c7"] = attempt(errors, f"{inst_id} c7", lambda: c7_reference(frames["1H"]))
    if market == "swap":
        out["liq_clusters"] = attempt(errors, f"{inst_id} liqmap", lambda: fetch_clusters(client, inst_id, frames["1H"]))
    return out


def protection(algos, inst_id, side, kind):
    for row in algos:
        if row.get("instId") != inst_id or row.get("posSide", "net") not in ("", "net", side):
            continue
        if row.get("side") and row["side"] != ("sell" if side == "long" else "buy"):
            continue
        trigger = number(row.get(kind + "TriggerPx"))
        if trigger is not None and trigger > 0:
            return {"algoId": row.get("algoId"), "trigger": trigger}
    return None


def fetch_account(client, state, errors):
    if not client.has_keys:
        errors.append("account: no API keys configured (dry mode)")
        return None
    balance = client.balance()[0]
    balances = {d["ccy"]: {"eq": number(d.get("eq")), "avail": number(d.get("availBal"))}
                for d in balance.get("details", [])}
    usdt = next((d for d in balance.get("details", []) if d["ccy"] == "USDT"), {})
    algos = []
    for kind in ("SWAP", "SPOT"):
        algos += attempt(errors, f"account {kind} algos", lambda: client.pending_algos(inst_type=kind), [])
    orders = attempt(errors, "account pending orders", lambda: client._req(
        "GET", "/api/v5/trade/orders-pending", private=True), None)
    positions = []
    for p in client.positions():
        size = number(p.get("pos")) or 0
        if not size:
            continue
        inst = p["instId"]
        side = p.get("posSide") if p.get("posSide") in ("long", "short") else "long" if size > 0 else "short"
        row = {"instId": inst, "market": "swap", "side": side, "size": abs(size),
               **{k: number(p.get(field)) for k, field in
                  (("avg_px", "avgPx"), ("last", "last"), ("mark", "markPx"), ("upl", "upl"),
                   ("lev", "lever"), ("margin", "margin"), ("liq_px", "liqPx"))},
               "state_record": state.get("positions", {}).get(inst)}
        row.update({k: protection(algos, inst, side, k) for k in ("sl", "tp")})
        positions.append(row)
    for d in balance.get("details", []):
        qty = number(d.get("cashBal", d.get("eq"))) or 0
        if d["ccy"] == "USDT" or qty <= 0:
            continue
        inst = d["ccy"] + "-USDT"
        spot = state.get("spot", {})
        rec = spot.get("longs", {}).get(inst) or spot.get("sleeves", {}).get(d["ccy"])
        positions.append({"instId": inst, "market": "spot", "side": "long", "size": qty,
                          "avg_px": number(d.get("accAvgPx")), "last": None, "mark": None,
                          "upl": number(d.get("spotUpl")), "lev": 1, "margin": None, "liq_px": None,
                          "sl": protection(algos, inst, "long", "sl"),
                          "tp": protection(algos, inst, "long", "tp"), "state_record": rec})
    return {"equity_usdt": number(balance.get("totalEq")),
            "avail_usdt": number(usdt.get("availEq") or usdt.get("availBal")),
            "positions": positions, "spot_balances": balances, "pending_orders": orders, "pending_algos": algos}


def normalized_url(url):
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")
             and k.lower() not in ("fbclid", "gclid")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(sorted(query)), ""))


def fetch_news(now, fetcher=None):
    items, errors = (fetcher or news.fetch_items)(max_age_h=24)
    out = []
    for item in items:
        try:
            published = dt.datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
            if published.tzinfo is None:
                raise ValueError("publication time has no timezone")
            age = (now - published).total_seconds() / 3600
            if not 0 <= age <= 24:
                continue
            title = html.unescape(item["title"]).strip()
            url = normalized_url(item.get("url") or "")
            summary = re.sub(r"<[^>]*>", " ", html.unescape(item.get("summary") or ""))
            summary = " ".join(summary.split())[:500]
            title_key = re.sub(r"[\W_]+", "", title.casefold())
            text = (title + " " + summary).casefold()
            tags = sorted(sym for sym, names in news.NAMES.items()
                          if any(re.search(r"\b" + re.escape(name) + r"\b", text) for name in names))
            out.append({"id": hashlib.sha1((url or title_key).encode()).hexdigest(), "title": title,
                        "source": item.get("src", item.get("source", "")), "url": url,
                        "published_at": iso(published), "age_h": round(age, 3), "summary": summary, "tags": tags})
        except Exception as exc:
            errors.append(f"news item: {exc}")
    seen_urls, seen_titles, unique = set(), set(), []
    for item in sorted(out, key=lambda i: (i["published_at"], i["id"]), reverse=True):
        title_key = re.sub(r"[\W_]+", "", item["title"].casefold())
        duplicate = (item["url"] and item["url"] in seen_urls) or title_key in seen_titles
        seen_urls.add(item["url"])
        seen_titles.add(title_key)
        if not duplicate:
            unique.append(item)
    return unique[:60], errors


def default_universe(client, state, account, errors):
    from okxbot.bot import Bot
    # Use just the existing read-only selector, bypassing Bot initialization/state mutation.
    selector = object.__new__(Bot)
    selector.x, selector.live, selector.log = client, client.has_keys, {}
    selector.ev = lambda *a, **kw: None
    selector.err = lambda message: errors.append("universe: " + message)
    selected = attempt(errors, "swap universe", lambda: selector.universe("SWAP"), [])
    from okxbot.spot import SpotManager
    selector.st = copy.deepcopy(state)
    spot = SpotManager(selector)
    def spot_universe():
        spot.load_market()
        return [i for i in sorted(spot.vol, key=lambda i: (-spot.vol[i], i))
                if i.split("-")[0] not in config.EXCLUDE_BASE][:config.SPOT_LONG_UNIVERSE]
    selected += attempt(errors, "spot universe", spot_universe, [])
    selected += [f"{base}-USDT-SWAP" for base in config.TRADFI_LIST]
    selected += list(state.get("positions", {})) + list(state.get("spot", {}).get("longs", {}))
    selected += [p["instId"] for p in (account or {}).get("positions", [])]
    return sorted(set(selected))


def fetch_events(now):
    if not Path(events.EVENTS_F).exists():
        raise FileNotFoundError("macro event calendar unavailable")
    return [{**{k: v for k, v in e.items() if k != "dt"}, "dt": iso(e["dt"])}
            for e in events.upcoming(hours=7 * 24, now=now) if e["dt"] >= now]


def finalize(snapshot):
    payload = {k: v for k, v in snapshot.items() if k not in ("snapshot_id", "sha256")}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    stamp = dt.datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00")).strftime("%Y%m%dT%H%M%SZ")
    return {**payload, "snapshot_id": f"snap-{stamp}-{digest[:10]}", "sha256": digest}


def build_snapshot(client, inst=None, no_news=False, now=None, state=None, news_fetcher=None):
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    errors = []
    client = CachedClient(client)
    if state is None:
        def read_state():
            path = Path(config.DATA_DIR, "state.json")
            return json.loads(path.read_text()) if path.exists() else {}
        state = attempt(errors, "state", read_state, {})
    state = copy.deepcopy(state)
    before = len(errors)
    account = attempt(errors, "account", lambda: fetch_account(client, state, errors))
    account_ok = account is not None and account.get("equity_usdt") is not None and len(errors) == before
    if account is not None and account.get("equity_usdt") is None:
        errors.append("account: equity unavailable")
    equity = (account or {}).get("equity_usdt")
    if equity is not None:
        today = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        if state.get("day") != today:
            state.update(day=today, day_start_equity=equity, day_locked=False)
        can_open, must_flatten, status = guardrail_status(state, equity)
    else:
        can_open, must_flatten, status = False, None, "account unavailable"
    guards = {"can_open": can_open, "must_flatten": must_flatten, "status": status,
              **{k: state.get(k) for k in ("day_start_equity", "peak_equity")},
              "halted": bool(state.get("halted")),
              **{k: getattr(config, k) for k in ("MAX_LEVERAGE", "MAX_MARGIN_PCT", "MAX_POSITIONS",
                 "RISK_PER_TRADE", "DAILY_LOSS_LIMIT", "MAX_DRAWDOWN", "MIN_STOP_PCT", "MAX_STOP_PCT")}}
    before = len(errors)
    ids = sorted(set(inst)) if inst is not None else default_universe(client, state, account, errors)
    specs, ticks, marks = {}, {}, {}
    for kind in sorted({"SWAP" if i.endswith("-SWAP") else "SPOT" for i in ids}):
        for target, name in ((specs, "instruments"), (ticks, "tickers"), (marks, "mark_prices")):
            if kind == "SPOT" and name == "mark_prices":
                continue
            rows = attempt(errors, f"{kind} {name}", lambda: getattr(client, name)(kind), [])
            target.update({r["instId"]: r for r in rows})
    instruments = {i: attempt(errors, i, lambda: fetch_instrument(client, i, specs, ticks, marks, now, errors)) for i in ids}
    market_ok = len(errors) == before
    for p in (account or {}).get("positions", []):
        data = instruments.get(p["instId"]) or {}
        p["last"] = p["last"] if p["last"] is not None else data.get("last")
        p["mark"] = p["mark"] if p["mark"] is not None else data.get("mark")
    news_items, news_errors = ([], []) if no_news else attempt(errors, "news", lambda: fetch_news(now, news_fetcher), ([], ["news unavailable"]))
    errors.extend(news_errors)
    before = len(errors)
    macro = attempt(errors, "events", lambda: fetch_events(now), [])
    event_ok = len(errors) == before
    candidates = [{"instId": i, "side": "long" if d["c7"]["signal"] == 1 else "short", "strength": d["c7"]["strength"]}
                  for i, d in instruments.items() if d and d.get("c7") and d["c7"]["signal"]]
    return finalize({"schema": "okxbot.snapshot/v1", "generated_at": iso(now),
                     "generated_at_local": now.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(), "demo": True,
                     "account": account, "account_available": account_ok, "guardrails": guards,
                     "instruments": instruments, "news": news_items, "events": macro, "c7_reference": candidates,
                     "completeness": {"account": account_ok, "market": market_ok,
                                      "news": not no_news and not news_errors, "events": event_ok}, "errors": errors})


def atomic_json(path, snapshot):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            name = handle.name
            json.dump(snapshot, handle, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def write_snapshot(snapshot, out=None):
    directory = Path(config.DATA_DIR, "snapshots")
    atomic_json(directory / (snapshot["snapshot_id"] + ".json"), snapshot)
    atomic_json(directory / "latest.json", snapshot)
    if out:
        atomic_json(out, snapshot)


def render_view(snapshot):
    lines = [f"OKX DEMO snapshot {snapshot['generated_at']} | {snapshot['guardrails']['status']}"]
    for inst, data in snapshot["instruments"].items():
        if not data:
            lines.append(f"{inst}: unavailable")
            continue
        c7 = data.get("c7") or {}
        lines.append(f"{inst} {data['market']} last={data['last']} mark={data['mark']} C7(ref)={c7.get('signal')}")
    for p in (snapshot["account"] or {}).get("positions", []):
        lines.append(f"POSITION {p['instId']} {p['side']} size={p['size']} avg={p['avg_px']} upl={p['upl']}")
    lines += [f"NEWS {n['published_at']} {n['source']}: {n['title']}" for n in snapshot["news"]]
    lines += ["ERROR " + error for error in snapshot["errors"]]
    return "\n".join(lines + [snapshot["snapshot_id"]])
