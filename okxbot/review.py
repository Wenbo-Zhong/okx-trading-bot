"""Decision review: structured decision fields + outcome attribution + baselines (Codex review #6, 2026-10-19).

Planner-supplied fields (optional, never block a trade; malformed fields are journaled as review_errors):
  plan JSON  "review": {...}             applies to the whole plan
  action     "review": {...}             overrides/extends the plan review for that action
  no-trade   --review '{...}'            (scripts.execute_plan --no-trade)
  {"category":     "news_event" | "trend_breakout" | "oscillation" | "other",
   "hypothesis":   "what should happen and why",
   "evidence":     ["fact 1", "fact 2"],
   "invalidation": "what proves it wrong (price level / event)",
   "horizon_h":    4,                      # holding / evaluation horizon, hours (0 < h <= 336)
   "cost_budget_usdt": 15,                 # max fees + slippage + funding accepted for the idea
   "watch": [{"instId": "BTC-USDT-SWAP", "side": "long", "stop": 81000}]}   # no-trade: what was passed on
Outcomes are attributed later from exchange data (journal_events close rows: realized PnL incl. fees and
funding). No-trade opportunity cost = forward return of each watched instrument over horizon_h (in R when a
stop is given) plus the C7-shadow trades that were available at that decision's snapshot.
Baselines over the same window and 0.5% risk per trade: C7 shadow (snapshot signals, stop/TP replayed on
later snapshot 1H bars), BTC buy-and-hold at risk-matched notional, cash (0)."""
import datetime as dt, glob, json, math, os, re
from okxbot import config

CATEGORIES = ("news_event", "trend_breakout", "oscillation", "other")
UTC = dt.timezone.utc
FEE, SLIP = 0.0005, 0.0003          # same per-side assumptions as backtests/backtest.py


def _txt(v):
    return isinstance(v, str) and bool(v.strip())


def normalize_review(obj):
    """Return (review_dict_or_None, errors). Unknown keys are dropped with an error; never raises."""
    if obj is None:
        return None, []
    if not isinstance(obj, dict):
        return None, ["review must be an object"]
    out, err = {}, []
    for k, v in obj.items():
        if k == "category":
            if v in CATEGORIES:
                out[k] = v
            else:
                err.append("category must be one of " + "/".join(CATEGORIES))
        elif k in ("hypothesis", "invalidation"):
            out[k] = v.strip() if _txt(v) else None
            if not _txt(v):
                err.append(k + " must be a non-empty string")
        elif k == "evidence":
            if isinstance(v, str) and v.strip():
                v = [v]
            if isinstance(v, list) and all(_txt(x) for x in v):
                out[k] = [x.strip() for x in v]
            else:
                err.append("evidence must be a list of strings")
        elif k in ("horizon_h", "cost_budget_usdt"):
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
            if k == "horizon_h" and ok and v > 336:
                ok = False
            if ok:
                out[k] = float(v)
            else:
                err.append(k + (" must be in (0, 336]" if k == "horizon_h" else " must be a positive number"))
        elif k == "watch":
            rows = []
            for w in (v if isinstance(v, list) else [None]):
                if isinstance(w, str) and w.strip():
                    w = {"instId": w.strip()}
                if not isinstance(w, dict) or not _txt(w.get("instId")) or w.get("side") not in (None, "long", "short"):
                    err.append("watch items need instId and optional side long/short"); continue
                r = {"instId": w["instId"], "side": w.get("side")}
                if isinstance(w.get("stop"), (int, float)) and not isinstance(w.get("stop"), bool) and w["stop"] > 0:
                    r["stop"] = float(w["stop"])
                rows.append(r)
            out[k] = rows
        else:
            err.append("unknown review field: " + str(k))
    return out, err


_KW = [("oscillation", r"震荡|区间|箱体|mm_glitch|range|oscillat|做市"),
       ("news_event", r"新闻|消息|事件|公告|ETF|CPI|GDP|非农|FOMC|美联储|关税|黑客|被盗|hack|exploit|news|headline"),
       ("trend_breakout", r"突破|破位|跌破|站上|C7|breakout|12h|趋势|排列|EMA")]


def infer_category(text):
    """Keyword guess for backfill only (reported as category_source='inferred')."""
    t = text or ""
    for cat, pat in _KW:
        if re.search(pat, t, re.I):
            return cat
    return "other"


# ---------- data loading ----------
def _jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return rows


def _ms(iso):
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def trades(events):
    """trade_id -> {open, closes, realized, fee, funding, closed}. Only trades with an open row."""
    out = {}
    for e in events:
        if e.get("ev") == "open" and e.get("trade_id"):
            out.setdefault(str(e["trade_id"]), {"open": e, "closes": []})
    for e in events:
        t = out.get(str(e.get("trade_id")))
        if t is not None and e.get("ev") == "close":
            t["closes"].append(e)
    for t in out.values():
        vals = [_f(c.get("realized_pnl")) for c in t["closes"]]
        vals = [v for v in vals if v is not None]
        t["realized"] = sum(vals) if vals else None
        t["fee"] = sum(_f(c.get("fee")) or 0 for c in t["closes"])
        t["funding"] = sum(_f(c.get("funding")) or 0 for c in t["closes"])
        rem = [c.get("remaining") for c in t["closes"] if "remaining" in c]
        t["closed"] = bool(t["closes"]) and (not rem or not _f(rem[-1]))
    return out


def decision_index(decisions):
    """order id -> decision row, plan_id -> decision row (executed plans only)."""
    by_order, by_plan = {}, {}
    for d in decisions:
        if d.get("claimed") or d.get("mode") != "execute":
            continue
        by_plan[d.get("plan_id")] = d
        for oid in (d.get("outcome") or {}).get("order_ids") or []:
            by_order[str(oid)] = d
    return by_order, by_plan


def decision_review(d, action_index=None):
    """Merged review for a decision (plan-level + action-level), plus where the category came from."""
    rv = dict(d.get("review") or {})
    if action_index is not None:
        for a in d.get("actions") or []:
            if a.get("index") == action_index and a.get("review"):
                rv.update(a["review"])
    if rv.get("category"):
        return rv, "planner"
    rv["category"] = infer_category(d.get("rationale"))
    return rv, "inferred"


def classify_trade(t, by_order, by_plan):
    o = t["open"]
    r = o.get("rationale") or {}
    d = by_order.get(str(o.get("trade_id"))) or by_plan.get(r.get("plan_id"))
    if d is not None:
        idx = next((a.get("index") for a in d.get("actions") or []
                    if str((a.get("result") or {}).get("ordId")) == str(o.get("trade_id"))), None)
        rv, src = decision_review(d, idx)
        return rv["category"], src, "planner", d, rv
    if "donchian_lookback_h" in r or "signal_bar_ts" in r:
        return "trend_breakout", "c7_rule", "c7_auto", None, {}
    return "other", "unknown", "other", None, {}


# ---------- price history from snapshots (offline, factual) ----------
def load_snapshots(snap_dir=None, t_from_ms=None):
    snap_dir = snap_dir or os.path.join(config.DATA_DIR, "snapshots")
    out = []
    for p in sorted(glob.glob(os.path.join(snap_dir, "snap-*.json"))):
        try:
            s = json.load(open(p, encoding="utf-8"))
            ts = _ms(s["generated_at"])
        except (OSError, ValueError, KeyError):
            continue
        if t_from_ms and ts < t_from_ms:
            continue
        inst = {}
        for k, v in (s.get("instruments") or {}).items():
            bars = ((v.get("candles") or {}).get("1H") or {}).get("bars") or []
            inst[k] = {"c7": v.get("c7") or {}, "last": _f(v.get("last")), "bars": bars,
                       "atr": _f((((v.get("candles") or {}).get("1H") or {}).get("indicators") or {}).get("atr14"))}
        out.append({"id": s.get("snapshot_id"), "ts": ts, "equity": _f((s.get("account") or {}).get("equity_usdt")),
                    "inst": inst})
    out.sort(key=lambda x: x["ts"])
    return out


def bar_series(snaps):
    """inst -> sorted list of 1H bars [ts,o,h,l,c] merged across snapshots (later snapshots win)."""
    m = {}
    for s in snaps:
        for k, v in s["inst"].items():
            d = m.setdefault(k, {})
            for b in v["bars"]:
                d[int(b[0])] = [int(b[0])] + [float(x) for x in b[1:5]]
    return {k: [d[t] for t in sorted(d)] for k, d in m.items()}


def _walk(series, t0, side, entry, stop, tp, t_end=None):
    """First exit on 1H bars strictly after t0 (bar open > t0 - 1h means bars that close after t0).
    Stop first when both touched. Returns (exit_px, exit_ts, why) or (last_close, ts, 'open')."""
    last = None
    for b in series:
        ts, o, h, l, c = b
        if ts + 3600_000 <= t0:
            continue
        if t_end and ts >= t_end:
            break
        last = b
        first = ts <= t0                      # bar that was forming at entry: stop counts, TP is not credited
        hit_sl = stop is not None and (l <= stop if side > 0 else h >= stop)
        hit_tp = not first and tp is not None and (h >= tp if side > 0 else l <= tp)
        if hit_sl:
            return (min(o, stop) if side > 0 else max(o, stop)) if ts > t0 else stop, ts, "stop"
        if hit_tp:
            return tp, ts, "tp"
    return (last[4], last[0], "open") if last else (entry, t0, "open")


def c7_shadow(snaps, series, t_from_ms=None, t_to_ms=None, risk=None, max_pos=None):
    """Follow every fresh C7 reference signal in the snapshots at the snapshot price with C7 stop/TP,
    0.5% risk of the snapshot equity, <= MAX_POSITIONS concurrent, one position per base coin."""
    risk = config.RISK_PER_TRADE if risk is None else risk
    max_pos = config.MAX_POSITIONS if max_pos is None else max_pos
    open_, done, seen = [], [], set()
    for s in snaps:
        if (t_from_ms and s["ts"] < t_from_ms) or (t_to_ms and s["ts"] > t_to_ms):
            continue
        open_ = [p for p in open_ if p["exit_ts"] + 3600_000 > s["ts"] or p["why"] == "open"]
        bases = {p["inst"].split("-")[0] for p in open_}
        for k, v in sorted(s["inst"].items()):
            sig, stop, tp, px = v["c7"].get("signal"), _f(v["c7"].get("stop")), _f(v["c7"].get("tp")), v["last"]
            if not sig or not stop or not px or len(open_) >= max_pos:
                continue
            bar_ts = int(v["bars"][-1][0]) if v["bars"] else s["ts"]
            base = k.split("-")[0]
            if sig < 0 and not k.endswith("-SWAP"):
                continue                                              # spot is long only
            if (k, bar_ts) in seen or base in bases or (sig > 0) != (stop < px):
                continue
            seen.add((k, bar_ts))
            eq = s["equity"] or 100000.0
            qty = eq * risk / abs(px - stop)
            qty = min(qty, eq / px)                                   # notional <= equity (5x * 20% margin)
            x, xts, why = _walk(series.get(k, []), s["ts"], 1 if sig > 0 else -1, px, stop, tp, t_to_ms)
            side = 1 if sig > 0 else -1
            gross = (x - px) * side * qty
            cost = qty * (px + x) * (FEE + SLIP) if why != "open" else qty * px * (FEE + SLIP)
            p = {"inst": k, "side": "long" if side > 0 else "short", "entry_ts": s["ts"], "entry": px, "stop": stop,
                 "tp": tp, "exit": x, "exit_ts": xts, "why": why, "net": gross - cost, "risk_usdt": eq * risk,
                 "snapshot_id": s["id"]}
            open_.append(p); done.append(p); bases.add(base)
    return done


def forward_return(series, t0, horizon_h, side=None):
    """(return_frac signed by side, end_ts) from first bar after t0 to the bar ~horizon later; None if no data."""
    if not series:
        return None
    start = next((b for b in series if b[0] + 3600_000 > t0), None)
    if start is None:
        return None
    t_end = t0 + horizon_h * 3600_000
    end = [b for b in series if b[0] + 3600_000 <= t_end + 1]
    if not end or end[-1][0] < start[0]:
        return None
    r = end[-1][4] / start[1] - 1
    return (r * (-1 if side == "short" else 1), end[-1][0] + 3600_000, t_end <= end[-1][0] + 3600_000)


def build(data_dir=None, since_ms=None, now_ms=None, snap_dir=None):
    data_dir = data_dir or config.DATA_DIR
    decisions = [d for d in _jsonl(os.path.join(data_dir, "decision_journal.jsonl"))
                 if d.get("kind") in ("plan", "no_trade") and not d.get("claimed")]
    events = _jsonl(os.path.join(data_dir, "journal_events.jsonl"))
    planner_start = min((_ms(d["ts"]) for d in decisions if d.get("ts")), default=None)
    since = since_ms or planner_start
    now_ms = now_ms or int(dt.datetime.now(UTC).timestamp() * 1000)
    by_order, by_plan = decision_index(decisions)
    T = trades(events)
    rows = []
    for tid, t in T.items():
        cat, src, origin, d, rv = classify_trade(t, by_order, by_plan)
        o = t["open"]
        risk = _f(o.get("planned_risk_usdt"))
        rows.append({"trade_id": tid, "instId": o.get("instId"), "side": o.get("side"), "open_ms": o.get("time_ms"),
                     "origin": origin, "category": cat, "category_source": src, "closed": t["closed"],
                     "realized": t["realized"], "fee": t["fee"], "funding": t["funding"],
                     "r": (t["realized"] / risk) if (t["realized"] is not None and risk) else None,
                     "plan_id": (d or {}).get("plan_id"), "review": rv})
    # decisions use snapshots up to PLAN_MAX_SNAPSHOT_AGE_MIN old -> include those just before the window
    snap_from = since - config.PLAN_MAX_SNAPSHOT_AGE_MIN * 60_000 if since else None
    snaps = load_snapshots(snap_dir or os.path.join(data_dir, "snapshots"), snap_from)
    series = bar_series(snaps)
    shadow = c7_shadow(snaps, series, snap_from, now_ms)
    shadow_all = c7_shadow(snaps, series, snap_from, now_ms, max_pos=10 ** 6)   # no position cap: less path-dependent
    # no-trade opportunity cost
    snap_by_id = {s["id"]: s for s in snaps}
    nt = []
    for d in decisions:
        if d.get("kind") != "no_trade" or not d.get("ts") or _ms(d["ts"]) < (since or 0):
            continue
        rv, src = decision_review(d)
        t0 = _ms(d["ts"])
        avail = [p for p in shadow if p["snapshot_id"] == d.get("snapshot_id")]
        watch = []
        for w in rv.get("watch") or []:
            fr = forward_return(series.get(w["instId"], []), t0, rv.get("horizon_h") or 0, w.get("side"))
            item = dict(w, horizon_h=rv.get("horizon_h"))
            if fr and rv.get("horizon_h"):
                item.update(fwd_return=fr[0], complete=fr[2])
                s0 = snap_by_id.get(d.get("snapshot_id"))
                px = ((s0 or {}).get("inst", {}).get(w["instId"]) or {}).get("last")
                if w.get("stop") and px:
                    item["fwd_r"] = fr[0] / (abs(px - w["stop"]) / px)
            watch.append(item)
        nt.append({"ts": d["ts"], "snapshot_id": d.get("snapshot_id"), "category": rv["category"],
                   "category_source": src, "c7_available": len(avail),
                   "c7_shadow_net": sum(p["net"] for p in avail), "watch": watch,
                   "has_review": bool(d.get("review"))})
    # buy-and-hold BTC, risk-matched notional
    hold = None
    btc = series.get("BTC-USDT-SWAP") or []
    win = [b for b in btc if since is None or b[0] + 3600_000 > since]
    if win:
        eq0 = next((s["equity"] for s in snaps if s["equity"]), None) or 100000.0
        stops = []
        for t in T.values():
            e, st = _f(t["open"].get("entry")), _f(t["open"].get("init_stop"))
            if e and st:
                stops.append(abs(e - st) / e)
        stop_pct = sorted(stops)[len(stops) // 2] if stops else config.MAX_STOP_PCT
        notional = min(eq0 * config.RISK_PER_TRADE / stop_pct, eq0)
        ret = win[-1][4] / win[0][1] - 1
        hold = {"notional": notional, "stop_pct_used": stop_pct, "btc_return": ret,
                "net": notional * ret - notional * 2 * (FEE + SLIP), "from_ms": win[0][0], "to_ms": win[-1][0] + 3600_000}
    closed_in_window = 0.0
    for t in T.values():
        for c in t["closes"]:
            v = _f(c.get("realized_pnl"))
            if v is not None and (since is None or int(c.get("time_ms") or 0) >= since):
                closed_in_window += v
    return {"since_ms": since, "planner_start_ms": planner_start, "closed_in_window": closed_in_window, "now_ms": now_ms, "trades": rows,
            "shadow": shadow, "shadow_uncapped": shadow_all, "no_trade": nt, "hold": hold, "snapshots": len(snaps),
            "decisions": {"plans_executed": sum(1 for d in decisions if d.get("mode") == "execute"),
                          "no_trade": sum(1 for d in decisions if d.get("kind") == "no_trade"),
                          "with_review": sum(1 for d in decisions if d.get("review"))}}


def summarize(res, origin="planner"):
    by = {}
    for r in res["trades"]:
        if origin and r["origin"] != origin:
            continue
        if origin == "planner" and r["open_ms"] and res["since_ms"] and r["open_ms"] < res["since_ms"]:
            continue
        b = by.setdefault(r["category"], {"n": 0, "closed": 0, "net": 0.0, "wins": 0, "r": [], "inferred": 0})
        b["n"] += 1
        b["inferred"] += r["category_source"] == "inferred"
        if r["realized"] is not None and r["closed"]:
            b["closed"] += 1; b["net"] += r["realized"]; b["wins"] += r["realized"] > 0
            if r["r"] is not None:
                b["r"].append(r["r"])
    return by
