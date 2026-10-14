"""Price/volatility, funding, macro-event and high-impact-news alerts.
Standalone: `python3 alerts.py` (prints an ALERTS: section + writes data/alerts.json).
Also called at the end of every `run_once.py` cycle. A state file (data/alerts_state.json) suppresses
repeats of the same alert key within config.ALERT_COOLDOWN_H hours (events/news use longer cooldowns)."""
import datetime as dt, json, os, time
import requests
import config
import events as macro_events
import funding

STATE_F = os.path.join(config.DATA_DIR, "alerts_state.json")
LAST_F = os.path.join(config.DATA_DIR, "alerts.json")
LOG_F = os.path.join(config.DATA_DIR, "alerts.jsonl")
MAJORS = {"BTC-USDT": (0.03, 0.06), "ETH-USDT": (0.03, 0.06)}   # (1h, 4h) thresholds
HELD_1H, HELD_4H = 0.05, 0.10


def _moves(inst_id):
    """(last, move_1h, move_4h) from 5-minute candles (production public data)."""
    r = requests.get(config.BASE_URL + "/api/v5/market/candles",
                     params={"instId": inst_id, "bar": "5m", "limit": 49}, timeout=10).json()
    rows = r.get("data") or []
    if len(rows) < 49:
        return None
    last = float(rows[0][4])
    return last, last / float(rows[12][4]) - 1, last / float(rows[48][4]) - 1


def held_instruments(st, positions):
    insts = {p["instId"]: "swap " + (p.get("posSide") or "") for p in positions}
    sp = st.get("spot", {})
    for coin, sl in sp.get("sleeves", {}).items():
        if sl.get("mode") == "IN":
            insts[f"{coin}-USDT"] = "spot holding"
    for inst in sp.get("longs", {}):
        insts[inst] = "spot long"
    return insts


def build(st, positions, news_high_impact=None, funding_rates=None, now=None):
    now = now or time.time()
    out = []
    # 1. price / volatility
    watch = {k: ("major",) + v for k, v in MAJORS.items()}
    for inst, why in held_instruments(st, positions).items():
        base = inst.replace("-SWAP", "")
        if base in watch:
            continue
        watch[inst] = (why, HELD_1H, HELD_4H)
    for inst, (kind, th1, th4) in watch.items():
        try:
            m = _moves(inst)
        except Exception:
            m = None
        if not m:
            continue
        last, m1, m4 = m
        for win, mv, th in (("1h", m1, th1), ("4h", m4, th4)):
            if abs(mv) >= th:
                out.append({"key": f"move:{inst}:{win}:{'up' if mv > 0 else 'down'}", "type": "price_move",
                            "msg": f"{inst} ({kind}) moved {mv * 100:+.2f}% in {win} (now {last:g}; threshold {th * 100:.0f}%)"})
    # 2. funding paid on open swaps
    if funding_rates is None:
        try:
            funding_rates = funding.all_rates()
        except Exception:
            funding_rates = {}
    for p in positions:
        fr = funding_rates.get(p["instId"])
        if not fr:
            continue
        side = 1 if p.get("posSide") == "long" or (p.get("posSide") == "net" and float(p["pos"]) > 0) else -1
        if funding.pays_funding(side, fr["rate_8h"]) and abs(fr["rate_8h"]) > config.FUNDING_MAX_PAY_8H:
            out.append({"key": f"funding:{p['instId']}:{'long' if side == 1 else 'short'}", "type": "funding", "cooldown_h": 8,
                        "msg": f"{p['instId']} {'long' if side == 1 else 'short'} is PAYING funding "
                               f"{fr['rate'] * 100:.4f}% per {fr['interval_h']:g}h (≈{fr['rate_8h'] * 100:.4f}%/8h)"})
    # 2b. estimated liquidation clusters near price (reference only; see liqmap.py)
    if getattr(config, "LIQMAP_ALERTS", False):
        try:
            import liqmap
            from okx_client import OKX
            x = OKX()
            insts = {"BTC-USDT-SWAP": "major", "ETH-USDT-SWAP": "major"}
            for p in positions:
                if p["instId"].endswith("-SWAP"):
                    insts[p["instId"]] = "swap " + (p.get("posSide") or "")
            for inst, kind in insts.items():
                lv, px = liqmap.current_levels(x, inst)
                for s, lo, hi, usd, d in liqmap.nearest_clusters(lv, px, span=config.LIQMAP_ALERT_SPAN, top=1):
                    what = "long-liquidation" if s == 1 else "short-liquidation"
                    amt = f"{usd / 1e6:.1f}M" if usd >= 1e6 else f"{usd / 1e3:.0f}k"
                    sp = (st.get("positions", {}).get(inst) or {}).get("stop")
                    note = ""
                    if sp:
                        note = "; OUR STOP %g is INSIDE it" % sp if lo <= sp <= hi else (
                            "; our stop %g is %.2f%% beyond it" % (sp, abs(sp / ((lo + hi) / 2) - 1) * 100))
                    out.append({"key": f"liq:{inst}:{s}:{round(lo / px, 2)}", "type": "liq_cluster", "cooldown_h": 6,
                                "msg": f"{inst} ({kind}): dense est. {what} cluster {lo:.6g}-{hi:.6g} ({d:+.2f}% from {px:g}, "
                                       f"~{amt} est. notional{note}; OI-based estimate, reference only)"})
        except Exception as ex:
            out.append({"key": "liq:error", "type": "error", "cooldown_h": 6, "msg": f"liq-cluster estimate failed: {ex}"})
    # 3. macro events (next 48h) and active risk windows
    try:
        for e in macro_events.upcoming(48):
            out.append({"key": f"event:{e['kind']}:{e['time_et']}", "type": "event", "cooldown_h": 12,
                        "msg": "Upcoming high-impact event: " + macro_events.fmt(e)})
        mode, e = macro_events.risk_window()
        if mode == "block":
            out.append({"key": f"eventblock:{e['time_et']}", "type": "event", "cooldown_h": 3,
                        "msg": "New entries BLOCKED (event window): " + macro_events.fmt(e)})
    except Exception as ex:
        out.append({"key": "event:error", "type": "error", "msg": f"event calendar error: {ex}"})
    # 4. high-impact news
    for h in news_high_impact or []:
        out.append({"key": "news:" + h["title"][:80], "type": "news", "cooldown_h": 24,
                    "msg": f"HIGH-IMPACT NEWS ({h['why']}; {h['age_h']}h ago, {h['src']}): {h['title']} -> new longs blocked"})
    return dedupe(out, now)


def dedupe(alerts, now):
    seen = json.load(open(STATE_F)) if os.path.exists(STATE_F) else {}
    fresh = []
    for a in alerts:
        cd = a.get("cooldown_h", config.ALERT_COOLDOWN_H) * 3600
        if now - seen.get(a["key"], 0) >= cd:
            fresh.append(a)
            seen[a["key"]] = now
    seen = {k: v for k, v in seen.items() if now - v < 7 * 86400}
    json.dump(seen, open(STATE_F, "w"), indent=1)
    ts = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")
    json.dump({"time": ts, "alerts": fresh}, open(LAST_F, "w"), indent=1, ensure_ascii=False)
    if fresh:
        with open(LOG_F, "a") as f:
            for a in fresh:
                f.write(json.dumps({"time": ts, **a}, ensure_ascii=False) + "\n")
    return fresh


def print_section(alerts):
    print("ALERTS:")
    for a in alerts:
        print(f"- [{a['type']}] {a['msg']}")


def main():
    from okx_client import OKX
    from bot import load_state
    import news
    st = load_state()
    x = OKX()
    positions = []
    if x.has_keys:
        try:
            positions = [p for p in x.positions() if float(p.get("pos") or 0) != 0]
        except Exception as e:
            print(f"(could not fetch positions: {e})")
    try:
        hi = news.sentiment([]).get("high_impact", [])
    except Exception:
        hi = []
    alerts = build(st, positions, hi)
    print_section(alerts)
    print(json.dumps(alerts, ensure_ascii=False))


if __name__ == "__main__":
    main()
