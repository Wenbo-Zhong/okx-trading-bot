"""Decision review report: net PnL by category, no-trade opportunity cost, baselines (C7 / hold / cash).

  python3 -m scripts.review_report [--since 2026-10-14] [--all] [--json] [--write]
Default window: from the first planner decision (planner mode). Read-only on exchange data: uses
data/decision_journal.jsonl, data/journal_events.jsonl and data/snapshots/*.json only.
--write appends one outcome row per decision to data/review_outcomes.jsonl (latest row per key wins)."""
import argparse, datetime as dt, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zoneinfo import ZoneInfo
from okxbot import config, review as RV

SH = ZoneInfo(config.TIMEZONE)


def t(ms):
    return dt.datetime.fromtimestamp(ms / 1000, SH).strftime("%m-%d %H:%M") if ms else "?"


def render(res, show_all=False):
    L = []
    L.append(f"=== Decision review {t(res['since_ms'])} -> {t(res['now_ms'])} (UTC+8) ===")
    d = res["decisions"]
    L.append(f"Decisions: {d['plans_executed']} executed plans, {d['no_trade']} no-trade; "
             f"{d['with_review']} carry structured review fields (others: category inferred from rationale)")
    for origin, title in (("planner", "Planner (LLM) trades opened in window"), ("c7_auto", "C7 auto trades (pre-planner)")):
        if origin == "c7_auto" and not show_all:
            continue
        L.append(f"\n{title} - net PnL by category (exchange realized, incl. fees + funding):")
        by = RV.summarize(res, origin)
        if not by:
            L.append("  none")
        for cat in RV.CATEGORIES:
            b = by.get(cat)
            if not b:
                continue
            avg_r = sum(b["r"]) / len(b["r"]) if b["r"] else None
            L.append(f"  {cat:<15} trades {b['n']:>3} closed {b['closed']:>3}  net {b['net']:+10.2f}  "
                     f"win {b['wins'] / b['closed'] * 100 if b['closed'] else 0:3.0f}%  "
                     f"avg R {avg_r:+.2f}" if avg_r is not None else
                     f"  {cat:<15} trades {b['n']:>3} closed {b['closed']:>3}  net {b['net']:+10.2f}")
            if b["inferred"]:
                L[-1] += f"  ({b['inferred']} inferred)"
    sh = res["shadow"]
    sh_closed = [p for p in sh if p["why"] != "open"]
    planner_net = sum(b["net"] for b in RV.summarize(res, "planner").values())
    L.append(f"\nBaselines over the same window, {config.RISK_PER_TRADE:.1%} risk per trade:")
    L.append(f"  Actual: planner-opened trades net {planner_net:+.2f}; all closes in window (incl. positions "
             f"opened earlier) {res['closed_in_window']:+.2f}")
    L.append(f"  C7 shadow ({res['snapshots']} snapshots): {len(sh)} trades ({len(sh_closed)} closed), "
             f"net {sum(p['net'] for p in sh):+.2f} (closed {sum(p['net'] for p in sh_closed):+.2f}, "
             f"open MTM {sum(p['net'] for p in sh if p['why'] == 'open'):+.2f}), "
             f"sum R {sum(p['net'] / p['risk_usdt'] for p in sh):+.2f}")
    su = res.get("shadow_uncapped") or []
    if su:
        L.append(f"  C7 shadow without the 3-position cap: {len(su)} trades, net {sum(p['net'] for p in su):+.2f}, "
                 f"sum R {sum(p['net'] / p['risk_usdt'] for p in su):+.2f} (capped result is path-dependent)")
    h = res["hold"]
    if h:
        L.append(f"  BTC buy & hold: BTC {h['btc_return'] * 100:+.2f}% on risk-matched notional "
                 f"{h['notional']:,.0f} (0.5% risk at median stop {h['stop_pct_used'] * 100:.2f}%) -> net {h['net']:+.2f}")
    L.append("  Cash: +0.00")
    nt = res["no_trade"]
    miss = [n for n in nt if n["c7_available"]]
    L.append(f"\nNo-trade opportunity cost: {len(nt)} no-trade decisions; {len(miss)} had a fresh C7 signal "
             f"available -> following C7 there would have netted {sum(n['c7_shadow_net'] for n in miss):+.2f}")
    w = [x for n in nt for x in n["watch"] if "fwd_return" in x]
    if w:
        rs = [x["fwd_r"] for x in w if "fwd_r" in x]
        L.append(f"  Watched setups passed on: {len(w)}; avg forward return {sum(x['fwd_return'] for x in w) / len(w) * 100:+.2f}%"
                 + (f", avg {sum(rs) / len(rs):+.2f} R" if rs else "") + " over the stated horizon")
    else:
        L.append("  Watched setups passed on: none recorded yet (needs review.watch + horizon_h on no-trade)")
    L.append("Notes: C7 shadow = snapshot C7 reference signals at snapshot price, C7 stop/TP walked on later "
             "snapshot 1H bars (stop first; TP not credited in entry bar), 0.05%+0.03% per side, no funding.")
    return "\n".join(L)


def outcomes(res):
    rows = []
    for r in res["trades"]:
        if r["plan_id"]:
            rows.append({"key": "trade:" + r["trade_id"], "plan_id": r["plan_id"], "category": r["category"],
                         "category_source": r["category_source"], "closed": r["closed"], "realized_pnl": r["realized"],
                         "fee": r["fee"], "funding": r["funding"], "r": r["r"]})
    for n in res["no_trade"]:
        rows.append({"key": "no_trade:" + str(n["snapshot_id"]), "ts": n["ts"], "category": n["category"],
                     "category_source": n["category_source"], "c7_available": n["c7_available"],
                     "c7_shadow_net": n["c7_shadow_net"], "watch": n["watch"]})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", help="YYYY-MM-DD (UTC+8); default first planner decision")
    ap.add_argument("--all", action="store_true", help="also show pre-planner C7 auto trades")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    since = int(dt.datetime.fromisoformat(a.since).replace(tzinfo=SH).timestamp() * 1000) if a.since else None
    res = RV.build(since_ms=since)
    if a.write:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with open(os.path.join(config.DATA_DIR, "review_outcomes.jsonl"), "a", encoding="utf-8") as f:
            for r in outcomes(res):
                f.write(json.dumps(dict(r, computed_at=now), ensure_ascii=False, default=str) + "\n")
    print(json.dumps(res, ensure_ascii=False, default=str) if a.json else render(res, a.all))
    return 0


if __name__ == "__main__":
    sys.exit(main())
