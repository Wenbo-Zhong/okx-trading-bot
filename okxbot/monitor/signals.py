"""Closed-bar signals with hysteresis, US-hours gating, and position-vs-plan checks. Pure functions + small state."""
import datetime as dt, math
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def us_market_open(now=None):
    """US cash session Mon-Fri 09:30-16:00 America/New_York (holidays not modelled)."""
    t = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ET)
    if t.weekday() >= 5:
        return False
    m = t.hour * 60 + t.minute
    return 9 * 60 + 30 <= m < 16 * 60


def ema(vals, n):
    k, e = 2 / (n + 1), None
    for v in vals:
        e = v if e is None else e + k * (v - e)
    return e


def atr(bars, n):
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for x in trs[n:]:
        a = (a * (n - 1) + x) / n
    return a


def median(v):
    s = sorted(v)
    return s[len(s) // 2] if s else 0


def metrics(bars, cfg):
    """bars: closed bars oldest first, dicts with ts,o,h,l,c,v. Last bar is the one being evaluated."""
    need = max(cfg["ema_slow"] + 5, cfg["breakout_lookback"] + 2, 75)
    if len(bars) < need:
        return None
    closes = [b["c"] for b in bars]
    last, prev = bars[-1], bars[:-1]
    a = atr(bars, cfg["atr_n"])
    lr = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - 73, len(closes) - 1) if closes[i - 1] > 0]
    hv = math.sqrt(sum(x * x for x in lr) / len(lr)) if lr else 0
    ret = math.log(last["c"] / prev[-1]["c"]) if prev[-1]["c"] > 0 else 0
    vols = [b["v"] for b in prev[-48:]]
    lb = prev[-cfg["breakout_lookback"]:]
    return {
        "close": last["c"], "atr": a,
        "trend": 1 if ema(closes, cfg["ema_fast"]) > ema(closes, cfg["ema_slow"]) else -1,
        "prev_trend": 1 if ema(closes[:-1], cfg["ema_fast"]) > ema(closes[:-1], cfg["ema_slow"]) else -1,
        "hi": max(b["h"] for b in lb), "lo": min(b["l"] for b in lb),
        "body_atr": abs(last["c"] - last["o"]) / a if a else 0,
        "rel_vol": last["v"] / (median(vols) or 1e-12),
        "hv_z": abs(ret) / hv if hv else 0,
        "ret_pct": ret * 100,
    }


class Armed:
    """Hysteresis latch: fires once when condition crosses, re-arms only after it falls back below rearm."""
    def __init__(self):
        self.fired = {}

    def check(self, key, trigger, rearm):
        if self.fired.get(key):
            if rearm:
                self.fired[key] = False
            return False
        if trigger:
            self.fired[key] = True
            return True
        return False


def evaluate(inst, bar, bars, group, cfg, latch, now=None):
    """Return list of events for a newly closed bar."""
    m = metrics(bars, cfg)
    if not m or not m["atr"]:
        return []
    if group == "stock" and not us_market_open(now):
        return []
    ev, k = [], f"{inst}:{bar}"
    anomaly = (m["rel_vol"] >= cfg["anomaly_rel_volume"]
               and (m["body_atr"] >= cfg["anomaly_body_atr"] or m["hv_z"] >= cfg["anomaly_hv_z"]))
    calm = m["rel_vol"] < cfg["rearm_rel_volume"] and m["body_atr"] < cfg["rearm_body_atr"]
    if latch.check(k + ":anomaly", anomaly, calm):
        direction = 1 if m["ret_pct"] >= 0 else -1
        ev.append(("price_volume_anomaly", m, "normal", direction))
    if group == "alt":
        return [_e(inst, bar, t, x, priority=pri, direction=d) for t, x, pri, d in ev]  # alts: anomaly only
    if m["trend"] != m["prev_trend"]:
        # trend flips are edge events already; latch per direction so a whipsaw needs a fresh flip
        if latch.check(f"{k}:trend:{m['trend']}", True, False):
            latch.fired[f"{k}:trend:{-m['trend']}"] = False
            # 15m flips need enough volume + body to wake; otherwise log as low priority
            pri = "normal"
            if bar == "15m":
                if (m["rel_vol"] < cfg.get("trend_change_15m_min_rel_vol", 1.0)
                        or m["body_atr"] < cfg.get("trend_change_15m_min_body_atr", 0.5)):
                    pri = "low"
            ev.append(("trend_change", m, pri, m["trend"]))
    ra = cfg["breakout_rearm_atr"] * m["atr"]
    if latch.check(k + ":bo_up", m["close"] > m["hi"], m["close"] < m["hi"] - ra):
        ev.append(("breakout_up", m, "normal", 1))
    if latch.check(k + ":bo_dn", m["close"] < m["lo"], m["close"] > m["lo"] + ra):
        ev.append(("breakout_down", m, "normal", -1))
    return [_e(inst, bar, t, x, priority=pri, direction=d) for t, x, pri, d in ev]


def _e(inst, bar, kind, m, priority="normal", direction=None):
    rec = {"kind": kind, "category": "bar", "inst": inst, "bar": bar,
           "priority": priority,
           "detail": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in m.items()}}
    if direction is not None:
        rec["direction"] = direction
    return rec


def position_checks(inst, pos, price, atr1h, cfg, latch, breach_since, now_s):
    """pos from state.json. Returns events; may flag severe fault if price sat beyond stop."""
    side, entry, stop = pos.get("side"), pos.get("entry"), pos.get("stop")
    if not (side and entry and stop and price):
        return []
    out = []
    risk = abs(entry - (pos.get("init_stop") or stop)) or None
    beyond = (price <= stop) if side > 0 else (price >= stop)
    if beyond:
        t0 = breach_since.setdefault(inst, now_s)
        if now_s - t0 >= cfg["stop_breach_grace_s"] and latch.check(inst + ":breach", True, False):
            out.append({"kind": "stop_not_triggered", "category": "fault", "severe": True, "inst": inst,
                        "detail": {"price": price, "stop": stop, "seconds_beyond": int(now_s - t0)}})
    else:
        breach_since.pop(inst, None)
        latch.check(inst + ":breach", False, True)
    if atr1h:
        dist = (price - stop) * side / atr1h
        if latch.check(inst + ":near_stop", 0 <= dist <= cfg["stop_proximity_atr"], dist > cfg["stop_proximity_rearm_atr"]):
            out.append({"kind": "near_stop", "category": "position", "inst": inst,
                        "detail": {"price": price, "stop": stop, "dist_atr": round(dist, 2)}})
    if risk:
        r = (price - entry) * side / risk
        if latch.check(inst + ":adverse", r <= -cfg["adverse_r"], r > -cfg["adverse_rearm_r"]):
            out.append({"kind": "adverse_move", "category": "position", "inst": inst,
                        "detail": {"price": price, "entry": entry, "r": round(r, 2)}})
    tp = pos.get("tp")
    if tp:
        hit = (price >= tp) if side > 0 else (price <= tp)
        if latch.check(inst + ":tp", hit, not hit):
            out.append({"kind": "tp_reached_still_open", "category": "position", "inst": inst,
                        "detail": {"price": price, "tp": tp}})
    return out
