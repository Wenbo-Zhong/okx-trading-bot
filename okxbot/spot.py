"""Spot module, run inside every run_once.py cycle (DEMO):
 1. Allocation sleeves for held coins (config.SPOT_ALLOC_COINS): hold the coin or park it in USDT
    using spot_alloc.decide (4h AND 1h trend must agree; min hours between switches).
 2. Optional C7 long-only entries on other liquid spot pairs, risk-sized, OCO stop/TP attached, no leverage.
No borrowing, no transfers, no withdrawals: only /trade/order, /trade/order-algo, /trade/amend-algos,
/trade/cancel-algos and /account/balance are used."""
import math, time, uuid
from okxbot import config
from okxbot import journal_log as J
from okxbot.okx_client import OKXError
from okxbot.indicators import to_df
from okxbot.spot_alloc import alloc_trends, decide
from okxbot.sizing import clamp_spot, depth_cap_usdt, fill_summary
from okxbot.strategy import build_features, initial_stop_tp, update_stop, should_exit_on_trend


def _rd(x, step):
    step = float(step)
    n = math.floor(x / step + 1e-9)
    dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, dec)


def _px(px, tick):
    tick = float(tick)
    dec = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return f"{round(round(px / tick) * tick, dec):.{dec}f}"


class SpotManager:
    def __init__(self, bot):
        self.b, self.x = bot, bot.x
        self.st = bot.st.setdefault("spot", {})
        self.st.setdefault("sleeves", {})
        self.st.setdefault("longs", {})
        self.st.setdefault("last_signal_ts", {})
        self.st.setdefault("pending_orders", {})
        self.inst, self.last, self.vol = {}, {}, {}

    def held_market(self, inst_id):
        """Load only a held instrument; use durable specs if the bulk market feed is down."""
        rec = self.st["longs"].get(inst_id, {})
        if inst_id not in self.inst:
            cached = rec.get("spec")
            self.inst[inst_id] = cached or self.x._req(
                "GET", "/api/v5/public/instruments", {"instType": "SPOT", "instId": inst_id},
                demo_public=bool(config.DEMO))[0]
        if inst_id not in self.last:
            try:
                self.last[inst_id] = float(self.x._req(
                    "GET", "/api/v5/market/ticker", {"instId": inst_id},
                    demo_public=bool(config.DEMO))[0]["last"])
            except Exception as e:
                self.b.err(f"held spot ticker {inst_id}: {e}")
        return self.inst[inst_id]

    def complete_order(self, ord_id):
        for key, pending in list(self.st["pending_orders"].items()):
            if pending.get("ordId") == ord_id:
                self.st["pending_orders"].pop(key)
        self.save_state()

    def recover_pending(self):
        """Resolve accepted/unknown buys without submitting a second order."""
        for key, pending in list(self.st["pending_orders"].items()):
            inst_id = pending["body"]["instId"]
            try:
                if pending.get("ordId"):
                    order = self.x.get_order(inst_id, pending["ordId"])[0]
                else:
                    order = self.x._req("GET", "/api/v5/trade/order",
                                        {"instId": inst_id, "clOrdId": key}, private=True)[0]
                    pending["ordId"] = order["ordId"]
                    self.save_state()
                if order.get("state") not in ("filled", "canceled", "mmp_canceled"):
                    self.x.cancel_order(inst_id, pending["ordId"])
                    order = self.x.get_order(inst_id, pending["ordId"])[0]
                    if order.get("state") not in ("filled", "canceled", "mmp_canceled"):
                        raise OKXError("buy not terminal after cancellation")
                qty, px = float(order.get("accFillSz") or 0), float(order.get("avgPx") or 0)
                if qty > 0:
                    if px <= 0:
                        raise OKXError("filled buy has no average price")
                    current = self.st["longs"].get(inst_id)
                    if current and current.get("ordId") != pending["ordId"]:
                        raise OKXError("recovered buy conflicts with a different tracked position")
                    if not current:
                        plan = pending["plan"]
                        self.inst[inst_id] = plan["spec"]
                        self.last[inst_id] = px
                        fee = float(order.get("fee") or 0)
                        qty = _rd(qty + (fee if order.get("feeCcy") == inst_id.split("-")[0] else 0),
                                  plan["spec"]["lotSz"])
                        stop, tp = initial_stop_tp(1, px, plan["atr"])
                        stop, tp = (stop, tp) if stop is not None else (plan["stop"], plan["tp"])
                        self.st["longs"][inst_id] = {
                            "qty": qty, "entry": px, "init_stop": stop, "stop": stop, "tp": tp,
                            "entry_ts": plan["bar_ts"] + 3600_000, "ordId": pending["ordId"],
                            "trade_id": pending["ordId"], "routed_from": plan.get("routed_from"),
                            "spec": plan["spec"], "planned_risk_usdt": (px - stop) * qty}
                        J.record("open", trade_id=pending["ordId"], instId=inst_id, market="spot",
                                 side="long", entry=px, size=qty, ct_val=1, lev=1, init_stop=stop, tp=tp,
                                 planned_risk_usdt=(px - stop) * qty,
                                 rationale={"recovered_pending_order": True})
                self.complete_order(pending["ordId"])
                self.b.ev("recovered spot order", instId=inst_id, ordId=pending["ordId"], filled=qty)
            except Exception as e:
                # A missing/unknown response is not proof that an order was never accepted.
                self.b.err(f"pending spot order {key}: {e}; new entries blocked")
        return not self.st["pending_orders"]

    # ---------- helpers ----------
    def save_state(self):
        from okxbot.bot import save_state
        save_state(self.b.st)

    def load_market(self):
        prod = {i["instId"]: i for i in self.x.instruments("SPOT")}
        demo_inst = {i["instId"]: i for i in self.x.instruments("SPOT", demo=True) if i.get("state") == "live"}
        demo = set(demo_inst)
        if self.b.live and config.DEMO:
            from okxbot.sizing import merge_exec_specs
            prod = merge_exec_specs(prod, demo_inst)
        for t in self.x.tickers("SPOT"):
            i = prod.get(t["instId"])
            if not i or t["instId"] not in demo or i.get("quoteCcy") != "USDT" or i.get("state") != "live":
                continue
            self.inst[t["instId"]] = i
            self.last[t["instId"]] = float(t["last"] or 0)
            self.vol[t["instId"]] = float(t.get("volCcy24h") or 0)

    def balances(self):
        bal = self.x.balance()[0]
        out = {}
        for d in bal.get("details", []):
            out[d["ccy"]] = {"avail": float(d.get("availBal") or 0), "eq": float(d.get("eq") or 0)}
        self.b.usdt_avail = out.get("USDT", {}).get("avail", 0.0)
        return out

    def candles(self, inst_id):
        df = to_df(self.x.recent_candles(inst_id, "1H", 720))
        return df[df["confirm"] == "1"].reset_index(drop=True)

    def fill(self, inst_id, ord_id, wait_s=8):
        o = {}
        for _ in range(int(wait_s / 0.5)):
            time.sleep(0.5)
            o = self.x.get_order(inst_id, ord_id)[0]
            if o.get("state") in ("filled", "canceled", "mmp_canceled"):
                break
        return o

    def market(self, inst_id, side, sz, quote=False, tag="", plan=None):
        body = {"instId": inst_id, "tdMode": "cash", "side": side, "ordType": "market", "sz": str(sz),
                "clOrdId": ("bs" + tag + uuid.uuid4().hex)[:32]}
        if quote:
            body["tgtCcy"] = "quote_ccy"
        if plan is not None:
            self.st["pending_orders"][body["clOrdId"]] = {"body": body, "plan": plan}
            self.save_state()  # intent survives a crash before the POST response
        res = self.x.place_order(body)[0]
        if res.get("sCode") not in (None, "", "0") or not res.get("ordId"):
            if plan is not None:
                self.st["pending_orders"].pop(body["clOrdId"], None)
                self.save_state()
            raise OKXError(f"spot order rejected: {res}")
        if plan is not None:
            self.st["pending_orders"][body["clOrdId"]]["ordId"] = res["ordId"]
        self.save_state()
        o = self.fill(inst_id, res["ordId"])
        if plan is not None and o.get("state") not in ("filled", "canceled", "mmp_canceled"):
            self.x.cancel_order(inst_id, res["ordId"])
            o = self.x.get_order(inst_id, res["ordId"])[0]
            if o.get("state") not in ("filled", "canceled", "mmp_canceled"):
                raise OKXError("spot buy still pending; recovery required")
        fsz, fpx = float(o.get("accFillSz") or 0), float(o.get("avgPx") or 0)
        fee, fee_ccy = float(o.get("fee") or 0), o.get("feeCcy")
        notional, partial = fill_summary(float(sz), fsz, fpx, quote=quote)
        out = {"ordId": res["ordId"], "state": o.get("state"), "sz": fsz, "px": fpx, "fee": fee, "feeCcy": fee_ccy,
               "notional": notional, "requested": float(sz), "requested_ccy": "quote" if quote else "base",
               "partial": partial, "cancelSource": o.get("cancelSource")}
        if partial:
            self.b.ev("PARTIAL FILL", instId=inst_id, side=side, **{k: out[k] for k in
                      ("ordId", "requested", "requested_ccy", "sz", "px", "notional", "state", "cancelSource")})
            self.b.log.setdefault("partial_fills", []).append({"instId": inst_id, **out})
            J.record("partial_fill", instId=inst_id, trade_id=res["ordId"], side=side, requested=float(sz),
                     requested_ccy=out["requested_ccy"], filled=fsz, avg_px=fpx, notional=notional,
                     state=o.get("state"), cancel_source=o.get("cancelSource"))
        return out

    def depth_cap(self, inst_id):
        """Max USDT to market-buy without walking the (demo) ask book more than SPOT_DEPTH_MAX_SLIP above mid."""
        try:
            bk = self.x._req("GET", "/api/v5/market/books", {"instId": inst_id, "sz": "400"}, demo_public=True)[0]
            mid = (float(bk["asks"][0][0]) + float(bk["bids"][0][0])) / 2
            return depth_cap_usdt(bk["asks"], mid, getattr(config, "SPOT_DEPTH_MAX_SLIP", 0.01))
        except Exception as e:
            self.b.err(f"orderbook {inst_id}: {e} (no depth cap)")
            return None

    def log(self, **row):
        from okxbot.bot import log_trade, now_local
        log_trade({"time": now_local().isoformat(timespec="seconds"), "mode": self.b.mode, **row})

    # ---------- 1. allocation sleeves ----------
    def run_alloc(self, can_rebuy):
        if not config.SPOT_ALLOC_ENABLED:
            return
        bals = self.balances() if self.b.live else {}
        now_ms = int(time.time() * 1000)
        summary = {}
        for coin in config.SPOT_ALLOC_COINS:
            inst_id = f"{coin}-USDT"
            if inst_id not in self.inst:
                self.b.err(f"spot alloc: {inst_id} not tradable on demo"); continue
            ins, px = self.inst[inst_id], self.last[inst_id]
            held = bals.get(coin, {}).get("avail", 0.0) if self.b.live else 0.0
            sl = self.st["sleeves"].get(coin)
            if sl is None:
                sl = {"mode": "IN" if held * px >= config.SPOT_MIN_ORDER_USDT or not self.b.live else "OUT",
                      "usdt": 0.0, "last_switch_ts": 0, "qty_at_start": held}
                self.st["sleeves"][coin] = sl
            try:
                df = alloc_trends(self.candles(inst_id))
            except Exception as e:
                self.b.err(f"spot alloc candles {inst_id}: {e}"); continue
            last = df.iloc[-1]
            t4, t1 = int(last["t4"]), int(last["t1"])
            hrs = (now_ms - sl["last_switch_ts"]) / 3.6e6
            action = decide(sl["mode"], t4, t1, hrs, config.SPOT_ALLOC_RULE)
            summary[coin] = {"mode": sl["mode"], "t4": t4, "t1": t1, "px": px, "held": held, "action": action}
            if action == "OUT":
                sz = _rd(held, ins["lotSz"])
                if sz < float(ins["minSz"]) or sz * px < config.SPOT_MIN_ORDER_USDT:
                    sl["mode"] = "OUT"; continue
                if not self.b.live:
                    self.b.ev("DRY-RUN spot sleeve would SELL", coin=coin, qty=sz, px=px, t4=t4, t1=t1); continue
                try:
                    f = self.market(inst_id, "sell", sz, tag="out")
                except OKXError as e:
                    self.b.err(f"spot sleeve sell {coin}: {e}"); continue
                if f["sz"] <= 0:
                    self.b.err(f"spot sleeve sell {coin} not filled ({f['state']})"); continue
                proceeds = f["sz"] * f["px"] + (f["fee"] if f["feeCcy"] == "USDT" else 0)
                sl.update({"mode": "OUT", "usdt": round(proceeds, 4), "last_switch_ts": now_ms,
                           "last_px": f["px"], "last_qty": f["sz"]})
                self.save_state()
                self.log(event="spot_sell", instId=inst_id, side="sell", contracts=f["sz"], price=f["px"],
                         notional=round(f["sz"] * f["px"], 2), reason=f"sleeve OUT: 4h={t4} 1h={t1}", ordId=f["ordId"])
                self.b.ev("spot sleeve SOLD to USDT", coin=coin, qty=f["sz"], px=f["px"], usdt=round(proceeds, 2))
                J.record("alloc", instId=inst_id, side="sell", qty=f["sz"], px=f["px"], fee=f["fee"], fee_ccy=f["feeCcy"],
                         notional=round(f["sz"] * f["px"], 2), reason=f"sleeve OUT: 4h={t4} 1h={t1}", ordId=f["ordId"])
            elif action == "IN":
                if not can_rebuy:
                    self.b.ev("spot sleeve rebuy blocked by guardrail", coin=coin); continue
                usdt_avail = self.b.usdt_avail if self.b.live else sl["usdt"]
                amt = round(min(sl["usdt"], usdt_avail * 0.95), 2)
                if amt < config.SPOT_MIN_ORDER_USDT:
                    self.b.ev("spot sleeve rebuy skipped: not enough free USDT", coin=coin, want=sl["usdt"],
                              usdt_avail=round(usdt_avail, 2)); continue
                if not self.b.live:
                    self.b.ev("DRY-RUN spot sleeve would BUY", coin=coin, usdt=amt, px=px); continue
                try:
                    f = self.market(inst_id, "buy", amt, quote=True, tag="in")
                except OKXError as e:
                    self.b.err(f"spot sleeve buy {coin}: {e}"); continue
                if f["sz"] <= 0:
                    self.b.err(f"spot sleeve buy {coin} not filled ({f['state']})"); continue
                sl.update({"mode": "IN", "usdt": 0.0, "last_switch_ts": now_ms, "last_px": f["px"], "last_qty": f["sz"]})
                self.save_state()
                self.log(event="spot_buy", instId=inst_id, side="buy", contracts=f["sz"], price=f["px"],
                         notional=amt, reason=f"sleeve IN: 4h={t4} 1h={t1}", ordId=f["ordId"])
                self.b.ev("spot sleeve BOUGHT back", coin=coin, qty=f["sz"], px=f["px"], usdt=amt)
                J.record("alloc", instId=inst_id, side="buy", qty=f["sz"], px=f["px"], fee=f["fee"], fee_ccy=f["feeCcy"],
                         notional=amt, reason=f"sleeve IN: 4h={t4} 1h={t1}", ordId=f["ordId"])
        self.b.log["spot_alloc"] = summary
        self.b.ev("spot allocation", **{c: f"{v['mode']} t4={v['t4']} t1={v['t1']} -> {v['action'] or 'hold'}"
                                        for c, v in summary.items()})
        if self.b.live:
            self.balances()   # refresh free USDT for swaps

    # ---------- 2. C7 spot longs ----------
    def sl_algo(self, inst_id):
        algos = self.long_algos(inst_id)
        return max(algos, key=lambda a: float(a.get("sz") or 0)) if algos else None

    def long_algos(self, inst_id):
        return [a for a in self.x.pending_algos(inst_id, "SPOT")
                if a.get("slTriggerPx") and a.get("side", "sell") == "sell"]

    def remember_algo(self, inst_id, result):
        for item in result:
            if item.get("sCode") not in (None, "", "0"):
                raise OKXError(f"spot protection rejected: {item}")
            if item.get("algoId"):
                rec = self.st["longs"][inst_id]
                ids = rec.setdefault("algo_ids", [])
                if item["algoId"] not in ids:
                    ids.append(item["algoId"])
        self.save_state()

    def reconcile_long(self, inst_id, balance):
        """Apply cumulative child-order fills once; balance changes never imply a full exit."""
        rec = self.st["longs"][inst_id]
        live = self.long_algos(inst_id)
        ids = rec.setdefault("algo_ids", [])
        for algo in live:
            if algo.get("algoId") and algo["algoId"] not in ids:
                ids.append(algo["algoId"])
        rec["exit_orders_pending"] = False
        seen = rec.setdefault("exit_fills", {})
        for algo_id in ids:
            try:
                algo = self.x.get_algo(algo_id)[0]
            except Exception as e:
                self.b.err(f"spot algo reconciliation {inst_id}/{algo_id}: {e}")
                self.b.protection_failed = True
                continue
            children = algo.get("ordIdList") or ([algo["ordId"]] if algo.get("ordId") else [])
            for ord_id in children:
                try:
                    order = self.x.get_order(inst_id, ord_id)[0]
                except Exception as e:
                    self.b.err(f"spot exit reconciliation {inst_id}/{ord_id}: {e}")
                    self.b.protection_failed = True
                    continue
                if order.get("state") not in ("filled", "canceled", "mmp_canceled"):
                    self.x.cancel_order(inst_id, ord_id)
                    order = self.x.get_order(inst_id, ord_id)[0]
                    if order.get("state") not in ("filled", "canceled", "mmp_canceled"):
                        rec["exit_orders_pending"] = True
                filled, avg = float(order.get("accFillSz") or 0), float(order.get("avgPx") or 0)
                old = seen.get(ord_id, {"size": 0, "notional": 0, "fee": 0})
                delta = filled - old["size"]
                if delta <= 1e-9:
                    continue
                unresolved = rec.get("unreconciled_qty", 0.0)
                if avg <= 0 or delta > rec["qty"] + unresolved + 1e-6:
                    raise OKXError(f"inconsistent exit fill for {ord_id}; keeping position")
                notional = filled * avg
                fee = float(order.get("fee") or 0) if order.get("feeCcy") == "USDT" else 0.0
                delta_fee = fee - old["fee"]
                px = (notional - old["notional"]) / delta
                rec["qty"] = max(0.0, rec["qty"] - max(0.0, delta - unresolved))
                rec["unreconciled_qty"] = max(0.0, unresolved - delta)
                J.record("close", instId=inst_id, trade_id=rec.get("trade_id") or rec.get("ordId"),
                         execution_id=f"{ord_id}:{filled:g}", source="exchange algo fill", size=delta,
                         remaining=rec["qty"], exit=px, fee=delta_fee,
                         realized_pnl=(px - rec["entry"]) * delta + delta_fee,
                         last_stop=rec.get("stop"), tp=rec.get("tp"))
                seen[ord_id] = {"size": filled, "notional": notional, "fee": fee}
                self.save_state()
        held = max(0.0, balance.get("eq", 0.0))
        if held + 1e-9 < rec["qty"]:
            # Legacy positions may lack algo IDs. Keep the real remainder and report the gap,
            # without inventing an execution price or PnL from a ticker snapshot.
            self.b.err(f"spot balance discrepancy {inst_id}: tracked {rec['qty']:g}, held {held:g}; PnL unresolved")
            rec["unreconciled_qty"] = rec.get("unreconciled_qty", 0.0) + rec["qty"] - held
            rec["qty"] = held
            rec["reconcile_gap"] = True
            self.b.protection_failed = True
        if rec["qty"] <= 1e-9 and rec.get("unreconciled_qty", 0.0) <= 1e-9:
            self.st["longs"].pop(inst_id)
        if rec.get("unreconciled_qty", 0.0) > 1e-9:
            self.b.protection_failed = True
        self.save_state()
        return inst_id in self.st["longs"]

    def protect_long(self, inst_id):
        """Repair quantity coverage before attempting indicator-based trailing."""
        from okxbot.bot import clamp_sl_trigger
        rec, ins = self.st["longs"][inst_id], self.inst[inst_id]
        algos = self.long_algos(inst_id)
        covered = sum(float(a.get("sz") or 0) for a in algos)
        missing = _rd(max(0.0, rec["qty"] - covered), ins["lotSz"])
        if covered + 1e-9 >= rec["qty"] or rec["qty"] < float(ins["minSz"]):
            return
        replace = missing < float(ins["minSz"])
        if replace:
            missing = _rd(rec["qty"], ins["lotSz"])
        ref = self.last.get(inst_id, 0)
        if ref <= 0:
            raise OKXError("no live reference for spot protection")
        stop, _, _ = clamp_sl_trigger(1, rec["stop"], ref)
        tick = float(ins["tickSz"])
        stop_px = _px(min(stop, (math.ceil(ref / tick) - 1) * tick), tick)
        result = self.x.place_algo({"instId": inst_id, "tdMode": "cash", "side": "sell",
                                   "ordType": "conditional", "sz": str(missing),
                                   "slTriggerPx": stop_px, "slOrdPx": "-1"})
        self.remember_algo(inst_id, result)
        for _ in range(4):
            visible = self.long_algos(inst_id)
            sufficient = (any(float(a.get("sz") or 0) + 1e-9 >= rec["qty"] for a in visible)
                          if replace else sum(float(a.get("sz") or 0) for a in visible) + 1e-9 >= rec["qty"])
            if sufficient:
                if replace and algos:
                    self.x.cancel_algos([{"algoId": a["algoId"], "instId": inst_id} for a in algos])
                return
            time.sleep(0.5)
        raise OKXError("spot stop quantity not confirmed")

    def exit_long(self, inst_id, reason):
        rec = self.st["longs"].get(inst_id)
        if not rec or not self.b.live:
            return
        from okxbot.bot import clamp_sl_trigger
        base = inst_id.split("-")[0]
        try:
            ins = self.held_market(inst_id)
        except Exception as e:
            self.b.err(f"spot long exit specs {inst_id}: {e}")
            self.b.protection_failed = True
            return
        min_sz = float(ins["minSz"])
        needs_stop = False
        ref_px = self.last.get(inst_id, 0)
        try:
            for a in self.x.pending_algos(inst_id, "SPOT"):
                self.x.cancel_algos([{"algoId": a["algoId"], "instId": inst_id}])
                needs_stop = True
            needs_stop = True  # also protect positions that had no algo
            avail = self.balances().get(base, {}).get("avail", 0.0)
            sz = _rd(min(avail, rec["qty"]), ins["lotSz"])
            if sz >= min_sz:
                f = self.market(inst_id, "sell", sz, tag="x")
                if f["sz"] > 0:
                    rec["qty"] = max(0.0, rec["qty"] - f["sz"])
                    self.save_state()
                    if f["px"] > 0:
                        ref_px = min(ref_px, f["px"]) if ref_px > 0 else f["px"]
                    pnl = (f["px"] - rec["entry"]) * f["sz"] + (f["fee"] if f["feeCcy"] == "USDT" else 0)
                    self.log(event="closed", instId=inst_id, side="long", contracts=f["sz"], price=f["px"],
                             pnl=round(pnl, 4), reason=f"spot long exit: {reason}", ordId=f["ordId"])
                    self.b.ev("spot long closed" if rec["qty"] < min_sz else "spot long partially closed",
                              instId=inst_id, reason=reason, qty=f["sz"], remaining=rec["qty"], pnl=round(pnl, 2))
                    J.record("close", instId=inst_id, trade_id=rec.get("trade_id") or rec.get("ordId"), source="bot exit",
                             execution_id=f"{f['ordId']}:{f['sz']:g}",
                             size=f["sz"], remaining=rec["qty"],
                             exit=f["px"], realized_pnl=round(pnl, 6), fee=f["fee"], fee_ccy=f["feeCcy"], reason=reason,
                             last_stop=rec.get("stop"), tp=rec.get("tp"), init_stop=rec.get("init_stop"))
        except OKXError as e:
            self.b.err(f"spot long exit {inst_id}: {e}")
        if not needs_stop:
            return
        if rec["qty"] < min_sz:
            if rec["qty"] > 0:
                self.b.ev("spot long dust dropped", instId=inst_id, qty=rec["qty"], minSz=min_sz)
            self.st["longs"].pop(inst_id, None)
            self.save_state()
            return
        try:
            if ref_px <= 0:
                raise OKXError("no market price for safe stop restoration")
            stop, _, _ = clamp_sl_trigger(1, rec["stop"], ref_px)
            tick = float(ins["tickSz"])
            # Nearest-tick formatting can round a clamped trigger back to market.
            stop_px = _px(min(stop, (math.ceil(ref_px / tick) - 1) * tick), tick)
            if not 0 < float(stop_px) < ref_px:
                raise OKXError("no valid non-marketable stop tick")
            result = self.x.place_algo({"instId": inst_id, "tdMode": "cash", "side": "sell", "ordType": "conditional",
                               "sz": str(_rd(rec["qty"], ins["lotSz"])), "slTriggerPx": stop_px, "slOrdPx": "-1"})
            self.remember_algo(inst_id, result)
            rec["stop"] = float(stop_px)
            self.save_state()
        except OKXError as e:
            self.b.err(f"spot long restore stop {inst_id}: {e}")
            self.b.protection_failed = True

    def manage_longs(self, must_flatten, why):
        if not self.st["longs"]:
            return
        bals = self.balances() if self.b.live else {}
        for inst_id, rec in list(self.st["longs"].items()):
            base = inst_id.split("-")[0]
            try:
                self.held_market(inst_id)
                if must_flatten:
                    self.exit_long(inst_id, "guardrail: " + why); continue
                if self.b.live:
                    if not self.reconcile_long(inst_id, bals.get(base, {})):
                        continue
                    if rec.get("exit_orders_pending"):
                        raise OKXError("exit child order still pending; new entries blocked")
                    if rec["qty"] < float(self.inst[inst_id]["minSz"]):
                        continue
                if self.b.live:
                    try:
                        self.protect_long(inst_id)
                    except Exception as e:
                        self.b.protection_failed = True
                        self.b.err(f"spot protection {inst_id}: {e}; exiting")
                        self.exit_long(inst_id, "could not confirm stop coverage")
                        continue
                f = build_features(self.candles(inst_id))
                last = f.iloc[-1]
                if should_exit_on_trend(1, last):
                    self.exit_long(inst_id, "trend flipped down"); continue
                since = f[f["ts"] >= rec["entry_ts"] - 3600_000]
                hi = max(rec["entry"], since["h"].max() if len(since) else rec["entry"])
                lo = min(rec["entry"], since["l"].min() if len(since) else rec["entry"])
                new = update_stop(1, rec["entry"], rec["init_stop"], rec["stop"], hi, lo, last["atr"])
                algo = self.sl_algo(inst_id) if self.b.live else None
                tick = self.inst[inst_id]["tickSz"]
                if self.b.live and algo is None:
                    self.b.err(f"spot long {inst_id} had no stop -> placing one")
                    self.x.place_algo({"instId": inst_id, "tdMode": "cash", "side": "sell", "ordType": "conditional",
                                       "sz": str(rec["qty"]), "slTriggerPx": _px(new, tick), "slOrdPx": "-1"})
                    self.save_state()
                elif algo and new > float(algo["slTriggerPx"]) * 1.0005:
                    self.x.amend_algo({"instId": inst_id, "algoId": algo["algoId"],
                                       "newSlTriggerPx": _px(new, tick), "newSlOrdPx": "-1"})
                    self.b.ev("spot long trailed stop", instId=inst_id, old=algo["slTriggerPx"], new=_px(new, tick))
                    J.record("stop", instId=inst_id, trade_id=rec.get("trade_id") or rec.get("ordId"),
                             old=float(algo["slTriggerPx"]), new=float(_px(new, tick)), kind="trail",
                             reason=f"chandelier trail {config.TRAIL_ATR_MULT}xATR after +{config.TRAIL_START_R}R")
                rec["stop"] = new
                self.b.ev("spot long holding", instId=inst_id, entry=rec["entry"], last=self.last.get(inst_id), stop=round(new, 8))
            except Exception as e:
                self.b.err(f"spot long manage {inst_id}: {e}")
                self.b.protection_failed = True

    def enter_longs(self, equity, news_scores, swap_insts):
        if not config.SPOT_LONGS_ENABLED:
            return
        slots = config.SPOT_MAX_LONGS - sum(1 for r in self.st["longs"].values() if not r.get("routed_from"))
        if slots <= 0:
            return
        skip = set(config.SPOT_ALLOC_COINS) | set(config.EXCLUDE_BASE) | {s.split("-")[0] for s in swap_insts}
        uni = [i for i in sorted(self.vol, key=lambda k: -self.vol[k])
               if i.split("-")[0] not in skip and i not in self.st["longs"]][:config.SPOT_LONG_UNIVERSE]
        cands = []
        for inst_id in uni:
            try:
                f = build_features(self.candles(inst_id))
            except Exception as e:
                self.b.err(f"spot candles {inst_id}: {e}"); continue
            if len(f) < 300:
                continue
            last = f.iloc[-1]
            if int(last["signal"]) != 1 or self.st["last_signal_ts"].get(inst_id) == int(last["ts"]):
                continue
            sym = inst_id.split("-")[0]
            if news_scores.get("coins", {}).get(sym, 0) < -config.NEWS_BLOCK_THRESHOLD or \
                    news_scores.get("market", 0) < -config.NEWS_BLOCK_THRESHOLD:
                continue
            cands.append((float(last["strength"]), inst_id, float(last["atr"]), int(last["ts"])))
            try:
                self.ctx = getattr(self, "ctx", {})
                self.ctx[inst_id] = {"signal_bar_ts": int(last["ts"]), "close": float(last["c"]),
                                     "breakout_level": float(last["hh20"]), "donchian_lookback_h": config.DONCHIAN_N,
                                     "trend_tf": config.TREND_TF, "trend": int(last["trend4h"]),
                                     "ema50": float(last["ema50"]), "ema200": float(last["ema200"]),
                                     "atr": float(last["atr"]), "atr_pct": round(float(last["atr"]) / float(last["c"]) * 100, 3),
                                     "rsi": round(float(last["rsi"]), 1), "strength": round(float(last["strength"]), 3),
                                     "news_coin": news_scores.get("coins", {}).get(sym, 0.0),
                                     "news_market": news_scores.get("market", 0.0)}
            except Exception:
                pass
        cands.sort(reverse=True)
        self.b.ev("spot long signals", candidates=[(c[1], round(c[0], 2)) for c in cands])
        for _, inst_id, atr_v, bar_ts in cands[:slots]:
            self.open_long(inst_id, atr_v, bar_ts, equity)

    def open_long(self, inst_id, atr_v, bar_ts, equity, routed_from=None, extra_rat=None):
        """Risk-sized spot market buy + OCO stop/TP. routed_from = swap instId when a ~1x swap long signal
        was routed here (config.SPOT_PREFER_FOR_LOW_LEV_LONGS). Returns True if a position was opened
        (or would be, in dry-run)."""
        if self.st["pending_orders"] or inst_id in self.st["longs"]:
            self.b.ev("spot entry skipped: held or unresolved order", instId=inst_id)
            return False
        ins, px = self.inst[inst_id], self.last[inst_id]
        stop, tp = initial_stop_tp(1, px, atr_v)
        if stop is None:
            self.b.ev("spot long skipped: stop too wide", instId=inst_id); return False
        stop_pct = (px - stop) / px
        risk_usdt = min(equity * config.SPOT_RISK_PER_TRADE / stop_pct, equity * config.MAX_MARGIN_PCT)
        exch_max = None
        if self.b.live:
            try:
                exch_max = float(self.x.max_size(inst_id, "cash")[0].get("maxBuy") or 0)
            except Exception as e:
                self.b.err(f"max-size {inst_id}: {e} (using balance only)")
        if self.b.live and getattr(config, "LIQ_FILTER_ENABLED", False):
            risk_usdt, ok_liq, _ = self.b.liquidity_check(inst_id, 1, px, stop, risk_usdt, 1.0, market="spot")
            if not ok_liq:
                self.st["last_signal_ts"][inst_id] = bar_ts
                return "fallback" if routed_from else False
        depth = self.depth_cap(inst_id) if self.b.live else None
        mkt_cap = min([v for v in (float(ins.get("maxMktAmt") or 0) or None, depth) if v is not None] or [0]) or None
        if routed_from and depth is not None and depth < risk_usdt * getattr(config, "SPOT_DEPTH_FALLBACK_FRAC", 0.5):
            self.b.ev("spot book too thin for routed long -> falling back to 1x swap", instId=inst_id,
                      planned_usdt=round(risk_usdt, 2), depth_cap_usdt=round(depth, 2))
            return "fallback"
        if depth is not None and depth < risk_usdt:
            self.b.ev("spot size capped by orderbook depth", instId=inst_id, planned_usdt=round(risk_usdt, 2),
                      depth_usdt_within_slip=round(depth / 0.8, 2), cap=round(depth, 2))
        notional, notes = clamp_spot(risk_usdt, self.b.usdt_avail if self.b.live else None, px, exch_max,
                                     mkt_cap, float(ins.get("minSz") or 0),
                                     config.SPOT_MIN_ORDER_USDT, getattr(config, "ORDER_SIZE_BUFFER", 0.95))
        if notes:
            self.b.ev("spot order size check", instId=inst_id, planned_usdt=round(risk_usdt, 2), notes=notes)
        if notional <= 0:
            self.b.ev("spot long skipped: not affordable / below minimum", instId=inst_id); return False
        self.st["last_signal_ts"][inst_id] = bar_ts
        tick = ins["tickSz"]
        if not self.b.live:
            self.b.ev("DRY-RUN spot long would open", instId=inst_id, usdt=notional, px=px,
                      stop=_px(stop, tick), tp=_px(tp, tick), routed_from=routed_from)
            return True
        try:
            f = self.market(inst_id, "buy", notional, quote=True, tag="l", plan={
                "atr": atr_v, "bar_ts": bar_ts, "stop": stop, "tp": tp,
                "routed_from": routed_from, "spec": ins})
            if f["sz"] <= 0:
                self.complete_order(f["ordId"])
                self.b.err(f"spot long {inst_id} not filled"); return False
            qty = f["sz"] + (f["fee"] if f["feeCcy"] == inst_id.split("-")[0] else 0)  # fee charged in base
            qty = _rd(qty, ins["lotSz"])
            stop, tp = initial_stop_tp(1, f["px"], atr_v) if initial_stop_tp(1, f["px"], atr_v)[0] else (stop, tp)
            self.st["longs"][inst_id] = {"qty": qty, "entry": f["px"], "init_stop": stop, "stop": stop, "tp": tp,
                                         "entry_ts": bar_ts + 3600_000, "ordId": f["ordId"],
                                         "routed_from": routed_from, "spec": ins}
            try:
                ew = self.b.log.get("event_window") or {}
                ctx = (getattr(self.b, "ctx", {}).get(routed_from) if routed_from else None) or \
                      (getattr(self, "ctx", {}).get(inst_id) or {})
                rat = {**ctx, "reconstructed": False, "liq_clusters_near": None, "event_window": ew.get("mode"),
                       "event": ew.get("event"), "size_mult_event": getattr(self.b, "size_mult", 1.0),
                       "size_mult_tradfi": 1.0, "routed_from_swap": routed_from, "size_notes": notes,
                       **(extra_rat or {})}
                rat.setdefault("funding", None)
                pr = (f["px"] - stop) * qty
                self.st["longs"][inst_id].update({"trade_id": f["ordId"], "rationale": rat, "planned_risk_usdt": round(pr, 4)})
                J.record("open", trade_id=f["ordId"], instId=inst_id, market="spot", side="long", entry=f["px"],
                         size=qty, ct_val=1.0, notional=round(qty * f["px"], 2), lev=1, init_stop=stop, tp=tp,
                         planned_risk_usdt=round(pr, 4), rationale=rat)
            except Exception as e:
                self.b.err(f"journal open {inst_id}: {e}")
            self.complete_order(f["ordId"])
            try:
                result = self.x.place_algo({"instId": inst_id, "tdMode": "cash", "side": "sell", "ordType": "oco", "sz": str(qty),
                                   "slTriggerPx": _px(stop, tick), "slOrdPx": "-1",
                                   "tpTriggerPx": _px(tp, tick), "tpOrdPx": "-1"})
                self.remember_algo(inst_id, result)
            except OKXError as e:
                self.b.err(f"spot long {inst_id}: stop placement failed ({e}) -> selling")
                self.exit_long(inst_id, "could not place stop"); return False
            why = "spot C7 breakout+trend" + (f" (routed from ~1x {routed_from} long)" if routed_from else "")
            self.log(event="open", instId=inst_id, side="long", contracts=qty, price=f["px"], stop=_px(stop, tick),
                     tp=_px(tp, tick), leverage=1, notional=round(qty * f["px"], 2),
                     reason=why + (f" PARTIAL FILL {f['notional']:.2f}/{notional:.2f} USDT" if f["partial"] else ""),
                     ordId=f["ordId"])
            self.b.ev("spot long opened", instId=inst_id, qty=qty, px=f["px"], usdt=round(qty * f["px"], 2),
                      requested_usdt=notional, partial=f["partial"], stop=_px(stop, tick), routed_from=routed_from)
            self.balances()
            return True
        except OKXError as e:
            self.b.err(f"spot long entry {inst_id}: {e}")
            return False
