"""Trade journal: builds data/journal.csv (one row per round trip, swap + spot, plus spot-allocation events)
and a Chinese Markdown review data/journal.md from data/journal_events.jsonl (written by bot.py/spot.py hooks
and journal_backfill.py), enriched with OKX positions/positions-history and production candles (MFE/MAE).
Read-only towards the exchange. Usage: python3 journal.py"""
import csv, datetime as dt, json, os
from zoneinfo import ZoneInfo
import numpy as np
from okxbot import config
from okxbot import journal_log as J
from okxbot.okx_client import OKX, OKXError
from okxbot.indicators import to_df

BJ = ZoneInfo("Asia/Shanghai")
CSV_F = os.path.join(config.DATA_DIR, "journal.csv")
MD_F = os.path.join(config.DATA_DIR, "journal.md")
DEC_F = os.path.join(config.DATA_DIR, "decisions.jsonl")
COLS = ["trade_id", "instId", "market", "intent", "side", "status", "open_time_bj", "close_time_bj", "holding_h", "entry", "exit",
        "size", "ct_val", "notional_usdt", "leverage", "init_stop", "tp", "final_stop", "stop_adjustments",
        "planned_risk_usdt", "realized_pnl_usdt", "price_pnl_usdt", "fee_usdt", "funding_usdt", "unrealized_pnl_usdt", "r_multiple",
        "mfe_r", "mae_r", "exit_reason", "possible_cause", "rationale_reconstructed", "rationale"]
EXIT_ZH = {"stop": "止损", "tp": "止盈", "trend_flip": "趋势反转", "trail": "移动止损", "manual": "手动/测试",
           "guardrail": "风控", "liquidation": "强平", "unknown": "未知", "open": "持仓中", "allocation": "仓位配置",
           "funding_flip": "资金费转正离场"}
INTENT_ZH = {"funding_harvest": "吃资金费率", "trend": "趋势突破", "spot_too_thin": "趋势突破(现货盘口太薄改永续)"}
MKT_ZH = {"crypto swap": "加密永续", "tradfi swap": "美股/商品永续", "spot": "现货", "spot allocation": "现货配置", "swap": "加密永续"}


def bj(ms):
    return dt.datetime.fromtimestamp(ms / 1000, BJ).strftime("%Y-%m-%d %H:%M") if ms else ""


def f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def mfe_mae(x, inst_id, side, entry, risk_px, t0, t1):
    """Max favourable / adverse excursion in R from 15m production candles during the hold (heuristic: bar extremes)."""
    if not risk_px or risk_px <= 0:
        return None, None
    n = int((t1 - t0) / 900_000) + 3
    try:
        rows = x.history_candles(inst_id, "15m", min(max(n, 4), 3000)) if n > 290 else x.candles(inst_id, "15m", min(max(n, 4), 300))
    except OKXError:
        return None, None
    df = to_df(rows)
    df = df[(df["ts"] + 900_000 > t0) & (df["ts"] <= t1)]
    if df.empty:
        return None, None
    hi, lo = float(df["h"].max()), float(df["l"].min())
    fav = (hi - entry) if side == 1 else (entry - lo)
    adv = (entry - lo) if side == 1 else (hi - entry)
    return round(fav / risk_px, 2), round(-adv / risk_px, 2)


def high_impact_runs():
    """[(time_ms, [titles])] from decision logs where high-impact negative news was active."""
    out = []
    if os.path.exists(DEC_F):
        for line in open(DEC_F):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            hi = (d.get("news") or {}).get("high_impact") or []
            if hi:
                out.append((int(dt.datetime.fromisoformat(d["time"]).timestamp() * 1000), [h.get("title") for h in hi]))
    return out


def macro_events_between(t0, t1, pad_h=2):
    try:
        from okxbot import events as E
        return [E.fmt(e) for e in E.load() if t0 - pad_h * 3.6e6 <= e["dt"].timestamp() * 1000 <= t1 + pad_h * 3.6e6]
    except Exception:
        return []


def bills_funding(x, inst, t0, t1=None):
    """Sum of funding-fee bills (type 8; + received, - paid) for inst in [t0, t1]. OKX keeps ~7 days here."""
    try:
        tot, after = 0.0, None
        for _ in range(10):
            p = {"instType": "SWAP", "type": "8", "instId": inst, "begin": str(int(t0)), "limit": "100"}
            if t1:
                p["end"] = str(int(t1))
            if after:
                p["after"] = after
            d = x._req("GET", "/api/v5/account/bills", p, private=True)
            if not d:
                break
            tot += sum(float(b.get("pnl") or b.get("balChg") or 0) for b in d)
            after = d[-1]["billId"]
            if len(d) < 100:
                break
        return round(tot, 6)
    except Exception:
        return None


def classify_exit(o, close, reqs, stops):
    if close is None:
        return "open"
    why = (close.get("reason") or "").lower()
    for r in reqs:
        why = why or (r.get("reason") or "").lower()
    if "funding_flip" in why:
        return "funding_flip"
    if "trend" in why:
        return "trend_flip"
    if "guardrail" in why or "daily loss" in why or "halt" in why:
        return "guardrail"
    if "smoke" in why or "manual" in why:
        return "manual"
    if str(close.get("close_type")) in ("3", "4"):
        return "liquidation"
    px, side = f(close.get("exit")), 1 if o["side"] == "long" else -1
    tp, stop = f(o.get("tp")), f(close.get("last_stop")) or (f(stops[-1]["new"]) if stops else f(o.get("init_stop")))
    if px and tp and abs(px / tp - 1) < 0.004:
        return "tp"
    if px and stop and abs(px / stop - 1) < 0.006:
        trailed = any(s.get("kind") == "trail" for s in stops) and (stop - f(o["init_stop"])) * side > 0
        return "trail" if trailed else "stop"
    return "unknown"


def causes(o, row, close_ms, stops, hi_runs, x):
    out = []
    side = 1 if o["side"] == "long" else -1
    ex, r, mfe = row["exit_reason"], f(row["r_multiple"]), f(row["mfe_r"])
    hold = f(row["holding_h"], 0)
    if ex == "stop" and hold <= 6 and (mfe is None or mfe < 0.5):
        out.append(f"入场后{hold:.1f}小时内止损且最大浮盈仅{mfe if mfe is not None else '?'}R，疑似假突破")
    if mfe is not None and mfe >= 1.0 and r is not None and r <= 0.2:
        out.append(f"持仓期间曾达到+{mfe}R，最终仅{r:+.2f}R，利润回吐")
    if ex == "trend_flip":
        out.append("1h趋势(EMA50/EMA200)反转触发离场")
    if ex == "tp":
        out.append("价格达到2R止盈目标")
    if ex == "trail" and r is not None and r > 0:
        out.append("移动止损锁定部分利润")
    nudges = [s for s in stops if s.get("kind") == "liq_nudge"]
    if nudges and ex in ("stop", "unknown") and r is not None and r < -1.0:
        out.append(f"清算簇止损外移{len(nudges)}次后被止损，亏损{r:+.2f}R超过1R（外移增加了亏损）")
    if nudges and close_ms is None:
        out.append(f"止损因估算清算簇外移{len(nudges)}次（最大亏损上限1.5R）")
    t0 = o["time_ms"]; t1 = close_ms or int(dt.datetime.now().timestamp() * 1000)
    hits = [t for t, _ in hi_runs if t0 <= t <= t1]
    if hits and close_ms and ex in ("stop", "trail"):
        out.append("持仓期间出现高影响负面新闻")
    ev = macro_events_between(t0, t1)
    if ev and close_ms and ex in ("stop", "trail"):
        out.append("离场前后2小时内有宏观事件: " + "; ".join(e.split(" [src")[0] for e in ev[:2]))
    rat = o.get("rationale") or {}
    if rat.get("news_coin") is not None and rat.get("news_coin") * side < -0.2:
        out.append(f"入场时该币新闻情绪与方向相反({rat['news_coin']:+.2f})")
    return ("（启发式）" + "；".join(out)) if out else ""


def aggregate_closes(events):
    """Combine exit executions; a partial close keeps the round trip open."""
    ordered, seen = [], set()
    for event in sorted(events, key=lambda e: e.get("close_time_ms") or e["time_ms"]):
        key = event.get("execution_id")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        ordered.append(event)
    if not ordered:
        return None
    result = dict(ordered[-1])
    for name in ("realized_pnl", "fee", "funding", "pnl"):
        values = [f(e.get(name)) for e in ordered if f(e.get(name)) is not None]
        result[name] = sum(values) if values else None
    sized = [e for e in ordered if f(e.get("size"), 0) > 0 and f(e.get("exit")) is not None]
    total = sum(f(e["size"]) for e in sized)
    if total:
        result["size"] = total
        result["exit"] = sum(f(e["size"]) * f(e["exit"]) for e in sized) / total
    return result


def build():
    x = OKX()
    ev = J.load()
    # A crash between journal append and state commit can replay the same recovered open.
    opens = list({e["trade_id"]: e for e in ev if e["ev"] == "open"}.values())
    allocs = [e for e in ev if e["ev"] == "alloc"]
    live_pos = {}
    if x.has_keys:
        try:
            live_pos = {p["instId"]: p for p in x.positions() if float(p.get("pos") or 0)}
        except OKXError:
            pass
    hi_runs = high_impact_runs()
    rows = []
    for o in sorted(opens, key=lambda e: e["time_ms"]):
        tid, inst = o["trade_id"], o["instId"]
        later_open = min([e["time_ms"] for e in opens if e["instId"] == inst and e["time_ms"] > o["time_ms"]] or [9e15])
        mine = lambda e: e.get("trade_id") == tid or (not e.get("trade_id") and e["instId"] == inst and o["time_ms"] <= e["time_ms"] < later_open)
        stops = sorted([dict(e, new=e.get("new", e.get("value")), kind=e.get("kind") or ("planner" if "value" in e else None))
                        for e in ev if e["ev"] == "stop" and mine(e)], key=lambda e: e["time_ms"])
        for i, s in enumerate(stops):   # planner amend_sl events carry only "value"; infer the old stop
            if s.get("old") is None:
                s["old"] = stops[i - 1]["new"] if i else o.get("init_stop")
        closes = [e for e in ev if e["ev"] == "close" and mine(e)]
        close = aggregate_closes(closes)
        fully_closed = close is not None and f(close.get("remaining"), 0) <= 1e-9
        reqs = [e for e in ev if e["ev"] == "close_request" and mine(e)]
        close_ms = (close.get("close_time_ms") or close["time_ms"]) if fully_closed else None
        side = 1 if o["side"] == "long" else -1
        ct = f(o.get("ct_val"), 1.0)
        risk_px = abs(f(o["entry"]) - f(o["init_stop"])) if o.get("init_stop") else None
        planned = f(o.get("planned_risk_usdt"))
        pnl = fee = fund = upl = None
        status = "closed" if fully_closed else "open"
        exit_px = f(close.get("exit")) if fully_closed else None
        if close:
            pnl, fee, fund = f(close.get("realized_pnl")), f(close.get("fee")), f(close.get("funding"))
        if not fully_closed and inst in live_pos:
            p = live_pos[inst]
            upl = f(p.get("upl"))
            if close is None:
                fee, fund = f(p.get("fee")), f(p.get("fundingFee"))
            exit_px = f(p.get("markPx"))
        elif not fully_closed and o["market"] == "spot":
            st = json.load(open(os.path.join(config.DATA_DIR, "state.json")))
            if inst in st.get("spot", {}).get("longs", {}):
                try:
                    exit_px = float(x._req("GET", "/api/v5/market/ticker", {"instId": inst})[0]["last"])
                    remaining = st["spot"]["longs"][inst]["qty"]
                    upl = (exit_px - f(o["entry"])) * remaining * side
                except Exception:
                    pass
            else:
                status = "closed (not recorded)"
        if fund is None and x.has_keys and o["market"] != "spot" and inst.endswith("-SWAP"):
            fund = bills_funding(x, inst, o["time_ms"], close_ms)
        if close:
            price_pnl = f(close.get("pnl"))
            if price_pnl is None and pnl is not None:
                price_pnl = pnl - (fee or 0) - (fund or 0)
        else:
            price_pnl = upl
        intent = o.get("intent") or (o.get("rationale") or {}).get("intent") or "trend"
        r_mult = round(pnl / planned, 2) if (pnl is not None and planned) else (round(upl / planned, 2) if (upl is not None and planned) else None)
        mfe, mae = mfe_mae(x, inst, side, f(o["entry"]), risk_px, o["time_ms"], close_ms or int(dt.datetime.now().timestamp() * 1000)) \
            if "smoke" not in json.dumps(o.get("rationale", {})) else (None, None)
        adj = "; ".join(f"{bj(s['time_ms'])} {s.get('kind')}: {s.get('old')}→{s.get('new')} ({s.get('reason')})" for s in stops)
        row = {"trade_id": tid, "instId": inst, "market": o["market"], "intent": intent, "side": o["side"], "status": status,
               "price_pnl_usdt": round(price_pnl, 4) if price_pnl is not None else None,
               "open_time_bj": bj(o["time_ms"]), "close_time_bj": bj(close_ms),
               "holding_h": round(((close_ms or int(dt.datetime.now().timestamp() * 1000)) - o["time_ms"]) / 3.6e6, 2),
               "entry": o["entry"], "exit": exit_px, "size": o["size"], "ct_val": ct, "notional_usdt": o.get("notional"),
               "leverage": o.get("lev"), "init_stop": o.get("init_stop"), "tp": o.get("tp"),
               "final_stop": stops[-1]["new"] if stops else o.get("init_stop"), "stop_adjustments": adj,
               "planned_risk_usdt": planned, "realized_pnl_usdt": pnl, "fee_usdt": fee, "funding_usdt": fund,
               "unrealized_pnl_usdt": round(upl, 4) if upl is not None else None, "r_multiple": r_mult,
               "mfe_r": mfe, "mae_r": mae, "exit_reason": classify_exit(o, close if fully_closed else None, reqs, stops),
               "rationale_reconstructed": bool((o.get("rationale") or {}).get("reconstructed")),
               "rationale": json.dumps(o.get("rationale") or {}, ensure_ascii=False, default=str)}
        row["possible_cause"] = causes(o, row, close_ms, stops, hi_runs, x)
        rows.append(row)
    for a in sorted(allocs, key=lambda e: e["time_ms"]):
        rows.append({"trade_id": a.get("ordId"), "instId": a["instId"], "market": "spot allocation", "side": a["side"],
                     "status": "event", "open_time_bj": bj(a["time_ms"]), "entry": a.get("px"), "size": a.get("qty"),
                     "notional_usdt": a.get("notional"), "leverage": 1, "exit_reason": "allocation",
                     "rationale": json.dumps({"reason": a.get("reason"), "reconstructed": bool(a.get("backfill"))}, ensure_ascii=False),
                     "rationale_reconstructed": bool(a.get("backfill")),
                     "possible_cause": "现货配置(sleeve)事件：按4h/1h趋势在持币与USDT间切换，不计入胜率"})
    with open(CSV_F, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS); w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") if r.get(k) is not None else "" for k in COLS})
    write_md(rows)
    return rows


def fmt(v, nd=2):
    return "-" if v in (None, "") else (f"{v:,.{nd}f}" if isinstance(v, (int, float)) else str(v))


def write_md(rows):
    trades = [r for r in rows if r["market"] != "spot allocation"]
    closed = [r for r in trades if r["status"] == "closed" and r.get("realized_pnl_usdt") is not None]
    strat = [r for r in closed if r["exit_reason"] != "manual"]
    opn = [r for r in trades if r["status"] == "open"]
    L = [f"# 交易日志（OKX 模拟盘）", f"生成时间：{dt.datetime.now(BJ):%Y-%m-%d %H:%M} 北京时间　|　DEMO=True", ""]
    L += ["## 汇总", ""]
    wins = [r for r in strat if r["realized_pnl_usdt"] > 0]
    rs = [r["r_multiple"] for r in strat if r.get("r_multiple") is not None]
    L += [f"- 交易笔数：共 {len(trades)} 笔（已平仓 {len(closed)}，其中策略交易 {len(strat)}、测试/手动 {len(closed) - len(strat)}；持仓中 {len(opn)}）；另有现货配置事件 {len(rows) - len(trades)} 次",
          f"- 胜率（已平仓策略交易）：{(len(wins) / len(strat) * 100):.0f}%" if strat else "- 胜率（已平仓策略交易）：暂无已平仓策略交易",
          f"- 平均R：{np.mean(rs):+.2f}R" if rs else "- 平均R：-",
          f"- 已实现盈亏合计：{sum(r.get('realized_pnl_usdt') or 0 for r in trades):+,.2f} USDT；持仓浮动盈亏：{sum(r.get('unrealized_pnl_usdt') or 0 for r in opn):+,.2f} USDT", ""]
    L += ["### 按市场类型", "", "| 市场 | 笔数(平/持) | 已实现 USDT | 浮动 USDT | 平均R(已平) |", "|---|---|---|---|---|"]
    for m in ["crypto swap", "tradfi swap", "spot"]:
        rr = [r for r in trades if r["market"] == m]
        if not rr:
            continue
        c = [r for r in rr if r["status"] == "closed"]
        rv = [r["r_multiple"] for r in c if r.get("r_multiple") is not None and r["exit_reason"] != "manual"]
        L.append(f"| {MKT_ZH[m]} | {len(c)}/{len(rr) - len(c)} | {sum(r.get('realized_pnl_usdt') or 0 for r in rr):+,.2f} | "
                 f"{sum(r.get('unrealized_pnl_usdt') or 0 for r in rr if r['status'] == 'open'):+,.2f} | {(f'{np.mean(rv):+.2f}' if rv else '-')} |")
    fh = [r for r in trades if r.get("intent") == "funding_harvest"]
    L += ["", "### 吃资金费率交易（1x 永续做多，资金费为负时多头收钱）", ""]
    if fh:
        L += ["| 币对 | 状态 | 价格盈亏 | 资金费盈亏 | 手续费 | 合计 |", "|---|---|---|---|---|---|"]
        for r in fh:
            pp, fu, fe = r.get("price_pnl_usdt") or 0, r.get("funding_usdt") or 0, r.get("fee_usdt") or 0
            L.append(f"| {r['instId']} | {'持仓中' if r['status'] == 'open' else '已平仓'} | {pp:+,.2f} | {fu:+,.2f} | {fe:+,.2f} | {pp + fu + fe:+,.2f} |")
        L.append(f"| 合计 | {len(fh)} 笔 | {sum(r.get('price_pnl_usdt') or 0 for r in fh):+,.2f} | {sum(r.get('funding_usdt') or 0 for r in fh):+,.2f} | "
                 f"{sum(r.get('fee_usdt') or 0 for r in fh):+,.2f} | - |")
    else:
        L.append("- 暂无")
    L += ["", "### 按离场原因", "", "| 离场原因 | 笔数 | 已实现 USDT | 平均R |", "|---|---|---|---|"]
    for k in sorted({r["exit_reason"] for r in trades}):
        rr = [r for r in trades if r["exit_reason"] == k]
        rv = [r["r_multiple"] for r in rr if r.get("r_multiple") is not None]
        L.append(f"| {EXIT_ZH.get(k, k)} | {len(rr)} | {sum(r.get('realized_pnl_usdt') or 0 for r in rr):+,.2f} | "
                 f"{(f'{np.mean(rv):+.2f}' if rv else '-')}{'（浮动）' if k == 'open' else ''} |")
    L += ["", "说明：R = 盈亏 / 计划风险（入场价到初始止损 × 仓位）。“可能原因”为规则自动生成的启发式判断，仅供复盘参考。"
          "标注“重建”的入场理由是事后用同一策略特征、当时的新闻得分、资金费率历史和OI估算清算图重建的，并非下单时记录。", ""]
    L += ["## 逐笔明细", ""]
    for r in trades:
        rat = json.loads(r["rationale"] or "{}")
        L += [f"### {r['instId']} {'做多' if r['side'] == 'long' else '做空'}{'【吃资金费率】' if r.get('intent') == 'funding_harvest' else ''}（{MKT_ZH[r['market']]}，{'持仓中' if r['status'] == 'open' else '已平仓'}）",
              f"- 编号 `{r['trade_id']}`｜开仓 {r['open_time_bj']}｜平仓 {r['close_time_bj'] or '-'}｜持仓 {r['holding_h']:.1f} 小时",
              f"- 入场均价 {r['entry']}｜{'现价/标记价' if r['status'] == 'open' else '出场均价'} {fmt(r['exit'], 6)}｜数量 {r['size']}（面值 {r['ct_val']}）｜名义 {fmt(r['notional_usdt'])} USDT｜杠杆 {r['leverage']}x",
              f"- 初始止损 {fmt(r['init_stop'], 6)}｜止盈 {fmt(r['tp'], 6)}｜当前/最终止损 {fmt(r['final_stop'], 6)}｜计划风险 {fmt(r['planned_risk_usdt'])} USDT (1R)",
              f"- 盈亏：已实现 {fmt(r['realized_pnl_usdt'], 4)}｜浮动 {fmt(r['unrealized_pnl_usdt'])}｜手续费 {fmt(r['fee_usdt'], 4)}｜资金费 {fmt(r['funding_usdt'], 4)} USDT｜R {fmt(r['r_multiple'])}｜MFE {fmt(r['mfe_r'])}R / MAE {fmt(r['mae_r'])}R",
              f"- 盈亏拆分：价格 {fmt(r.get('price_pnl_usdt'), 4)}｜资金费 {fmt(r['funding_usdt'], 4)}｜手续费 {fmt(r['fee_usdt'], 4)} USDT｜意图 {INTENT_ZH.get(r.get('intent'), r.get('intent'))}",
              f"- 离场原因：{EXIT_ZH.get(r['exit_reason'], r['exit_reason'])}"]
        if r["stop_adjustments"]:
            L.append(f"- 止损调整：{r['stop_adjustments']}")
        tag = "（重建）" if r["rationale_reconstructed"] else ""
        if rat.get("note") and rat.get("breakout_level") is None:
            L.append(f"- 入场理由{tag}：{rat['note']}")
        elif rat:
            tr = {1: "多头", -1: "空头", 0: "中性"}.get(rat.get("trend"), "?")
            fr = rat.get("funding") or {}
            frs = (f"{fr.get('rate_8h', fr.get('last_settled_rate', 0)) * 100:+.4f}%" if fr else "-")
            liq = rat.get("liq_clusters_near")
            liqs = "；".join(f"{c[0]} {c[1]:g}-{c[2]:g} ({c[4]:+.2f}%)" for c in liq) if isinstance(liq, list) and liq else ("-" if not liq else str(liq))
            L += [f"- 入场理由{tag}：{rat.get('donchian_lookback_h')}h 唐奇安{'突破' if r['side'] == 'long' else '跌破'} {fmt(rat.get('breakout_level'), 6)}（信号K线收盘 {fmt(rat.get('close'), 6)}）；"
                  f"{rat.get('trend_tf')}趋势{tr}（EMA50 {fmt(rat.get('ema50'), 6)} / EMA200 {fmt(rat.get('ema200'), 6)}）；ATR {fmt(rat.get('atr'), 6)}（{fmt(rat.get('atr_pct'))}%）；RSI {fmt(rat.get('rsi'), 1)}",
                  f"- 环境{tag}：新闻情绪 币种 {fmt(rat.get('news_coin'))} / 大盘 {fmt(rat.get('news_market'))}；资金费率 {frs}；宏观事件窗口 {rat.get('event_window') or '无'}{(' ' + rat['event']) if rat.get('event') else ''}；"
                  f"仓位系数 事件×{rat.get('size_mult_event', 1)} / TradFi×{rat.get('size_mult_tradfi', 1)}",
                  f"- 附近估算清算簇（±5%）：{liqs}"]
            if rat.get("note"):
                L.append(f"- 备注：{rat['note']}")
        if r["possible_cause"]:
            L.append(f"- 可能原因：{r['possible_cause']}")
        L.append("")
    sk = [e for e in J.load() if e["ev"] == "skip"][-30:]
    if sk:
        L += ["## 因盘口太薄跳过的信号（最近30条）", "", "| 时间 | 品种 | 方向 | 计划名义 | 1.5%内深度 入场侧/止损侧 | 预估止损滑点 |", "|---|---|---|---|---|---|"]
        for e in sk:
            L.append(f"| {bj(e['time_ms'])} | {e['instId']} | {'做多' if e.get('side') == 'long' else '做空'} | {fmt(e.get('notional_after_slip'))} | "
                     f"{fmt(e.get('depth_entry_usdt'))} / {fmt(e.get('depth_stop_usdt'))} | {fmt(e.get('est_stop_slip_pct'))}% |")
        L.append("")
    al = [r for r in rows if r["market"] == "spot allocation"]
    if al:
        L += ["## 现货配置事件（不计入胜率）", "", "| 时间 | 币对 | 方向 | 数量 | 成交价 | 金额 USDT | 依据 |", "|---|---|---|---|---|---|---|"]
        for r in al:
            L.append(f"| {r['open_time_bj']} | {r['instId']} | {'卖出→USDT' if r['side'] == 'sell' else '买回'} | {r['size']} | {fmt(r['entry'])} | "
                     f"{fmt(r['notional_usdt'])} | {json.loads(r['rationale'])['reason']}{'（重建）' if r['rationale_reconstructed'] else ''} |")
    open(MD_F, "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    rows = build()
    print(f"journal: {len(rows)} rows -> {CSV_F}, {MD_F}")
