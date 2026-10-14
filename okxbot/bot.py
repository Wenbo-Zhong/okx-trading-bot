"""One decision cycle of the OKX demo trading bot."""
import csv, datetime as dt, json, math, os, time, traceback, uuid
from zoneinfo import ZoneInfo
from okxbot import config
from okxbot.okx_client import OKX, OKXError
from okxbot.indicators import to_df
from okxbot.strategy import build_features, initial_stop_tp, update_stop, should_exit_on_trend
from okxbot.risk import size_position, guardrail_status
from okxbot import news
from okxbot.spot import SpotManager
from okxbot import events as macro_events
from okxbot import funding
from okxbot import tradfi
from okxbot import liqmap
from okxbot import sizing
from okxbot import journal_log as J

os.makedirs(config.DATA_DIR, exist_ok=True)
STATE_F = os.path.join(config.DATA_DIR, "state.json")
DECISIONS_F = os.path.join(config.DATA_DIR, "decisions.jsonl")
TRADES_F = os.path.join(config.DATA_DIR, "trades.csv")
EQUITY_F = os.path.join(config.DATA_DIR, "equity.csv")
HALT_F = os.path.join(config.DATA_DIR, "HALTED")
TRADE_COLS = ["time", "event", "instId", "side", "contracts", "price", "stop", "tp", "leverage",
              "notional", "pnl", "reason", "ordId", "mode"]


def now_local():
    return dt.datetime.now(ZoneInfo(config.TIMEZONE))


def load_state():
    if os.path.exists(STATE_F):
        return json.load(open(STATE_F))
    return {"positions": {}, "last_signal_ts": {}, "start_equity": None}


def save_state(st):
    tmp = STATE_F + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_F)


def log_trade(row):
    new = not os.path.exists(TRADES_F)
    with open(TRADES_F, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in TRADE_COLS})


def log_equity(equity, mode, n_pos):
    new = not os.path.exists(EQUITY_F)
    with open(EQUITY_F, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "equity", "open_positions", "mode"])
        w.writerow([now_local().isoformat(timespec="seconds"), round(equity, 4), n_pos, mode])


def bot_equity(st, positions):
    """The bot trades a virtual sub-account of START_CAPITAL USDT inside the (larger) demo account:
    equity = START_CAPITAL + realized PnL of bot trades + unrealized PnL of open positions.
    This keeps guardrails/sizing on the 500U mandate and immune to other holdings' price moves."""
    return config.START_CAPITAL + float(st.get("realized_pnl", 0.0)) + \
        sum(float(p.get("upl") or 0) for p in positions)


def round_step(x, step, down=True):
    step = float(step)
    n = math.floor(x / step + 1e-9) if down else round(x / step)
    dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, dec)


def fmt_px(px, tick):
    return f"{round_step(px, tick, down=False):.{max(0, -int(math.floor(math.log10(float(tick))))) if float(tick) < 1 else 0}f}"



def clamp_sl_trigger(side, stop, last_px, mark_px=None, buffer_pct=None):
    """Keep a stop-loss on the valid side of live last AND mark with a small buffer.

    OKX rejects SL triggers that are already marketable:
      - protective BUY (covering a short): SL must be strictly above last and mark
      - protective SELL (exiting a long): SL must be strictly below last and mark
    Amend-algos validates against last (sCode 51278); place with slTriggerPxType=mark
    validates against mark (51302). We enforce both. Returns (stop, clamped:bool, reason|None).
    """
    if buffer_pct is None:
        buffer_pct = float(getattr(config, "SL_TRIGGER_BUFFER_PCT", 0.001))
    refs = [float(p) for p in (last_px, mark_px) if p is not None and float(p) > 0]
    if not refs:
        return stop, False, None
    lo, hi = min(refs), max(refs)
    if side == 1:   # long -> SL below market
        ceiling = lo * (1.0 - buffer_pct)
        if stop >= ceiling:
            return ceiling, True, f"long SL {stop:.8g} >= live min(last,mark) {lo:.8g}; clamped to {ceiling:.8g}"
        return stop, False, None
    floor = hi * (1.0 + buffer_pct)
    if stop <= floor:
        return floor, True, f"short SL {stop:.8g} <= live max(last,mark) {hi:.8g}; clamped to {floor:.8g}"
    return stop, False, None


def live_sl_refs(pos):
    """Demo-aware last/mark from a /account/positions row (preferred over production candles)."""
    last = float(pos.get("last") or 0) or None
    mark = float(pos.get("markPx") or 0) or None
    return last, mark


class Bot:
    def __init__(self):
        self.x = OKX()
        self.live = self.x.has_keys
        self.mode = "demo" if self.live else "dry-run"
        self.st = load_state()
        if self.st.get("mode") not in (None, self.mode):   # never mix dry-run and demo state
            os.replace(STATE_F, STATE_F + f".{self.st.get('mode')}.bak")
            self.st = load_state()
        self.st["mode"] = self.mode
        self.swaps_blocked = False
        self.block_longs = False
        self.size_mult = 1.0
        self.protection_failed = False
        self.funding = {}
        self.usdt_avail = float("inf")
        self.log = {"time": now_local().isoformat(timespec="seconds"), "mode": self.mode,
                    "events": [], "errors": []}

    def ev(self, msg, **kw):
        self.log["events"].append({"msg": msg, **kw})
        print(f"[{self.mode}] {msg}" + (f" {kw}" if kw else ""))

    def err(self, msg):
        self.log["errors"].append(msg)
        print(f"[{self.mode}] ERROR {msg}")

    # ---------------- data ----------------
    def universe(self, inst_type="SWAP"):
        tick = self.x.tickers(inst_type)
        inst = {i["instId"]: i for i in self.x.instruments(inst_type)}
        try:
            demo_inst = {i["instId"]: i for i in self.x.instruments(inst_type, demo=True) if i.get("state") == "live"}
            demo_ids = set(demo_inst)
        except Exception as e:
            if self.live and config.DEMO:
                raise RuntimeError(f"demo instrument specs unavailable ({e}); refusing to size orders off production specs")
            demo_inst, demo_ids = {}, set(inst); self.err(f"demo instrument list failed: {e}")
        if self.live and config.DEMO:
            inst = sizing.merge_exec_specs(inst, demo_inst)   # ctVal/lotSz/... from the venue that executes
        rows = []
        for t in tick:
            i = inst.get(t["instId"])
            if not i or i.get("state") != "live" or t["instId"] not in demo_ids:
                continue
            if inst_type == "SWAP":
                if i.get("instCategory") != "1" or i.get("settleCcy") != "USDT" or i.get("ctType") != "linear":
                    continue
                base = t["instId"].split("-")[0]
                vol_usdt = float(t["volCcy24h"] or 0) * float(t["last"] or 0)
            else:  # SPOT
                if i.get("quoteCcy") != "USDT":
                    continue
                base = i.get("baseCcy") or t["instId"].split("-")[0]
                vol_usdt = float(t.get("volCcy24h") or 0)  # already quote volume for spot
            if base in config.EXCLUDE_BASE:
                continue
            rows.append((vol_usdt, t["instId"], float(t["last"]), i))
        rows.sort(reverse=True)
        self.instr = {r[1]: r[3] for r in rows}
        self.last_px = {r[1]: r[2] for r in rows}
        self.inst_type = inst_type
        top = rows[:config.UNIVERSE_SIZE]
        self.ev("universe", instType=inst_type, top=[r[1] for r in top])
        out = [r[1] for r in top]
        if inst_type == "SWAP" and getattr(config, "TRADFI_ENABLED", False):
            # US stock / commodity perps: known to the bot for management always; tradable only if the
            # demo mark is within TRADFI_MAX_MARK_DEV of production and 24h volume is high enough.
            tick_d = {t["instId"]: t for t in tick}
            for b in config.TRADFI_LIST:
                iid = f"{b}-USDT-SWAP"
                if iid in inst and iid in tick_d:
                    self.instr[iid] = inst[iid]
                    self.last_px[iid] = float(tick_d[iid]["last"] or 0)
            try:
                ok, skipped, info = tradfi.eligible(self.x, inst, tick_d)
                self.log["tradfi"] = {"ok": ok, "skipped": skipped, "session": tradfi.session()}
                self.ev("tradfi universe", ok=ok, skipped=skipped, session=tradfi.session())
                out += [i for i in ok if i not in out]
            except Exception as e:
                self.err(f"tradfi eligibility check failed (skipping stock/commodity perps): {e}")
        return out

    def features(self, inst_id):
        rows = self.x.recent_candles(inst_id, "1H", 720)
        df = to_df(rows)
        df = df[df["confirm"] == "1"].reset_index(drop=True)   # closed candles only
        if len(df) < 300:
            return None
        return build_features(df)

    # ---------------- account ----------------
    def account(self):
        if not self.live:
            eq = self.st.get("paper_equity", config.START_CAPITAL)
            return eq, []
        cfg = self.x.account_config()[0]
        self.pos_mode = cfg.get("posMode", "net_mode")
        self.acct_lv = cfg.get("acctLv")
        if self.acct_lv == "1":
            # Demo learning: auto-switch to Multi-currency margin so swaps work and totalEq is usable.
            try:
                self.x.set_account_level(3)
                self.acct_lv = "3"
                self.ev("switched account mode Spot -> Multi-currency margin (acctLv=3)")
            except OKXError as e:
                try:
                    self.x.set_account_level(2)
                    self.acct_lv = "2"
                    self.ev("switched account mode Spot -> Futures mode (acctLv=2)")
                except OKXError as e2:
                    self.swaps_blocked = True
                    self.ev("Account is Spot mode; API cannot switch (OKX 51070). "
                            "Falling back to SPOT long-only. To enable SWAP+leverage: OKX Demo app "
                            "-> Trade settings -> Futures or Multi-currency margin.")
        bal = self.x.balance()[0]
        total_eq = float(bal.get("totalEq") or 0)
        self.log["account_totalEq"] = total_eq
        usdt = next((d for d in bal.get("details", []) if d["ccy"] == "USDT"), {})
        self.usdt_avail = float(usdt.get("availEq") or usdt.get("availBal") or 0)
        pos = [p for p in self.x.positions() if float(p.get("pos") or 0) != 0]
        # Demo + DEMO_USE_FULL_EQUITY: size off whole-account totalEq (~102k).
        # Later (live): set DEMO_USE_FULL_EQUITY=False so sizing caps at LIVE_EQUITY_CAP (500).
        if not self.live:
            equity = config.START_CAPITAL
        elif config.DEMO and config.DEMO_USE_FULL_EQUITY:
            equity = total_eq
        else:
            equity = min(total_eq, config.LIVE_EQUITY_CAP)
        return equity, pos

    def pos_side(self, side):
        if getattr(self, "pos_mode", "net_mode") == "long_short_mode":
            return "long" if side == 1 else "short"
        return None

    # ---------------- actions ----------------
    def close(self, inst_id, pos_side, reason):
        if not self.live:
            return
        try:
            is_spot = "-SWAP" not in inst_id and getattr(self, "inst_type", "SWAP") == "SPOT"
            if is_spot or self.swaps_blocked:
                rec = self.st["positions"].get(inst_id, {})
                sz = rec.get("contracts")
                if sz is None:
                    # fall back to full base balance
                    base = inst_id.split("-")[0]
                    bal = self.x.balance()[0]
                    d = next((x for x in bal.get("details", []) if x["ccy"] == base), {})
                    sz = float(d.get("availBal") or d.get("eq") or 0)
                if float(sz) <= 0:
                    self.err(f"close spot {inst_id}: no size"); return
                self.cancel_algos(inst_id)
                self.x.place_order({"instId": inst_id, "tdMode": "cash", "side": "sell",
                                    "ordType": "market", "sz": str(sz),
                                    "clOrdId": "botc" + uuid.uuid4().hex[:19]})
                save_state(self.st)
            else:
                self.x.close_position(inst_id, pos_side)
                save_state(self.st)
                self.cancel_algos(inst_id)
            self.ev("closed position", instId=inst_id, reason=reason)
            J.record("close_request", instId=inst_id, reason=reason,
                     trade_id=(self.st.get("positions", {}).get(inst_id) or {}).get("trade_id"))
            log_trade({"time": now_local().isoformat(timespec="seconds"), "event": "close_request",
                       "instId": inst_id, "reason": reason, "mode": self.mode})
        except OKXError as e:
            self.err(f"close {inst_id}: {e}")

    def cancel_algos(self, inst_id):
        try:
            algos = self.x.pending_algos(inst_id)
            if algos:
                self.x.cancel_algos([{"algoId": a["algoId"], "instId": inst_id} for a in algos])
        except OKXError as e:
            self.err(f"cancel algos {inst_id}: {e}")

    def sl_algos(self, inst_id):
        return sorted([a for a in self.x.pending_algos(inst_id) if a.get("slTriggerPx")],
                      key=lambda a: -float(a.get("sz") or 0))

    def sl_algo(self, inst_id):
        algos = self.sl_algos(inst_id)
        return algos[0] if algos else None

    def place_protective_sl(self, inst_id, side, contracts, stop, pos_side):
        tick = self.instr[inst_id]["tickSz"]
        is_spot = "-SWAP" not in inst_id
        body = {"instId": inst_id, "tdMode": "cash" if is_spot else config.TD_MODE,
                "side": "sell" if side == 1 else "buy",
                "ordType": "conditional", "sz": str(contracts),
                "slTriggerPx": fmt_px(stop, tick), "slOrdPx": "-1",
                "slTriggerPxType": "last" if is_spot else "mark"}
        if not is_spot:
            body["reduceOnly"] = True
            if pos_side:
                body["posSide"] = pos_side
                body.pop("reduceOnly")
        result = self.x.place_algo(body)
        save_state(self.st)
        return result

    def ensure_sl_coverage(self, inst_id, side, size, stop, pos_side, lot):
        """Count matching stops and verify quantity repairs on the executing venue."""
        def matching():
            exit_side = "sell" if side == 1 else "buy"
            return [a for a in self.sl_algos(inst_id)
                    if a.get("side", exit_side) == exit_side
                    and a.get("posSide", pos_side or "net") == (pos_side or "net")]
        algos = matching()
        covered = sum(float(a.get("sz") or 0) for a in algos)
        if covered + 1e-9 >= size:
            return algos
        missing = round_step(size - covered, lot)
        try:
            if missing <= 0:
                raise OKXError("uncovered size cannot be rounded to a valid lot")
            result = self.place_protective_sl(inst_id, side, missing, stop, pos_side)
            if any(a.get("sCode") not in (None, "", "0") for a in result):
                raise OKXError(f"stop rejected: {result}")
            for _ in range(4):
                algos = matching()
                if sum(float(a.get("sz") or 0) for a in algos) + 1e-9 >= size:
                    self.ev("stop coverage repaired", instId=inst_id, size=size)
                    return algos
                time.sleep(0.5)
            raise OKXError("stop quantity not confirmed on exchange")
        except Exception as e:
            self.protection_failed = True
            self.err(f"stop coverage {inst_id}: {e}; closing position and blocking entries")
            self.close(inst_id, pos_side, "could not confirm stop coverage")
            return None

    def liq_nudge_open(self, inst_id, side, rec, stop, price, atr, last_px=None, mark_px=None):
        """Liquidation-cluster nudge for an OPEN position (reference only). Returns dict or None.
        Constraints: max loss at the new stop <= LIQMAP_MAX_RISK_MULT x original planned risk (entry->init_stop),
        loss-side distance <= MAX_STOP_PCT from entry, and new stop must not be marketable vs live last/mark.
        `price` is the live reference for cluster geometry (prefer mark/last, not a stale candle close)."""
        try:
            lv, _ = liqmap.current_levels(self.x, inst_id)
        except Exception as e:
            self.err(f"liqmap {inst_id}: {e}"); return None
        cand, info = liqmap.adjust_stop(side, price, stop, atr, lv, near_toward_atr=liqmap.NEAR_BELOW_ATR, max_check=False)
        if info is None or cand == stop:
            return None
        entry, init = float(rec["entry"]), float(rec.get("init_stop") or stop)
        planned = abs(entry - init)
        loss = max(0.0, (entry - cand) * side)          # price-distance loss at the new stop (same size)
        ratio = loss / planned if planned > 0 else float("inf")
        why = None
        if ratio > config.LIQMAP_MAX_RISK_MULT + 1e-9:
            why = f"max loss {ratio:.2f}x planned > {config.LIQMAP_MAX_RISK_MULT}x"
        elif loss / entry > config.MAX_STOP_PCT:
            why = f"loss distance {loss / entry:.2%} > MAX_STOP_PCT"
        else:
            # Reject if the nudged stop would already be marketable on the live venue (demo last/mark).
            chk, bad, reason = clamp_sl_trigger(side, cand, last_px if last_px is not None else price,
                                               mark_px if mark_px is not None else price)
            if bad:
                why = "would trigger immediately vs live last/mark"
        res = {"old_stop": round(stop, 8), "new_stop": cand, "clusters": info["clusters"], "price": price,
               "risk_ratio": round(ratio, 2), "planned_risk_px": round(planned, 8), "new_loss_px": round(loss, 8)}
        if why:
            self.ev("liq nudge rejected (stop kept)", instId=inst_id, reason=why, **res)
            self.log.setdefault("stop_adjustments_rejected", []).append({"instId": inst_id, "reason": why, **res})
            return None
        self.ev("liq nudge: moving stop beyond cluster", instId=inst_id, **res)
        return res

    def replace_sl(self, inst_id, side, size, algo, new_stop, pos_side, tick, last_px=None, mark_px=None):
        """Amend the SL; verify it on the exchange; fallback = place new SL first, then cancel old.
        Returns (ok, effective_stop). ok True if a stop at effective_stop is confirmed live.
        Never leaves the position without a stop. Pre-checks live last/mark so we do not send a
        marketable SL (OKX 51278/51302). Soft-skips (event, not error) when the old stop remains."""
        new_stop, clamped, reason = clamp_sl_trigger(side, new_stop, last_px, mark_px)
        if clamped:
            self.ev("SL clamp before amend", instId=inst_id, reason=reason, stop=new_stop)
        # If clamp pushed the stop to/through the wrong side of the live exchange stop, skip.
        ex_stop = float(algo["slTriggerPx"])
        still_tighter = (side == 1 and new_stop > ex_stop * 1.0005) or (side == -1 and new_stop < ex_stop * 0.9995)
        if not still_tighter:
            self.ev("stop amend skipped (old SL kept)", instId=inst_id,
                    reason="clamped SL is not tighter than live exchange stop",
                    proposed=new_stop, exchange_stop=ex_stop, last=last_px, mark=mark_px)
            return False, ex_stop
        px = fmt_px(new_stop, tick)
        effective = float(px)
        def confirmed():
            for _ in range(4):
                time.sleep(0.5)
                for a in self.sl_algos(inst_id):
                    if (a.get("side", "sell" if side == 1 else "buy") == ("sell" if side == 1 else "buy")
                            and a.get("posSide", pos_side or "net") == (pos_side or "net")
                            and abs(float(a["slTriggerPx"]) - float(px)) <= float(tick) * 1.01
                            and float(a.get("sz") or 0) >= size * 0.999):
                        return a
            return None
        try:
            self.x.amend_algo({"instId": inst_id, "algoId": algo["algoId"], "newSlTriggerPx": px, "newSlOrdPx": "-1"})
            if confirmed():
                return True, effective
            self.ev(f"amend SL {inst_id}: not visible after amend; replacing")
        except OKXError as e:
            self.ev(f"amend SL {inst_id} failed ({e}); replacing")
        try:
            self.place_protective_sl(inst_id, side, size, effective, pos_side)
        except OKXError as e:
            # Old protective SL still live — soft skip, do not fail the cycle.
            self.ev(f"replacement SL {inst_id} failed ({e}); old stop kept")
            return False, ex_stop
        if confirmed():
            try:
                still = [a for a in self.sl_algos(inst_id) if a["algoId"] == algo["algoId"]]
                if still:
                    self.x.cancel_algos([{"algoId": algo["algoId"], "instId": inst_id}])
            except OKXError as e:
                self.err(f"cancel old SL {inst_id}: {e} (two stops live; next cycle removes redundant)")
            return True, effective
        self.ev(f"replacement SL {inst_id} not confirmed; old stop kept")
        return False, ex_stop

    # ---------------- main cycle ----------------
    def run(self):
        st = self.st
        try:
            equity, positions = self.account()
        except OKXError as e:
            self.err(f"account fetch failed: {e}")
            return self.finish()
        # Prefer SWAP; if Spot-mode blocks perps, fall back to SPOT (long-only) so demo can still learn.
        self.inst_type = "SPOT" if self.swaps_blocked else "SWAP"
        self.instr, self.last_px = {}, {}
        try:
            if self.swaps_blocked:
                self.ev("SWAP blocked by account mode -> trading SPOT (long-only, 1x) until Futures/Multi-currency is enabled in OKX Demo UI")
                positions = []  # spot holdings tracked via state, not /account/positions
                # rebuild open spot positions from state against balances
                if self.live:
                    bal = self.x.balance()[0]
                    held = {d["ccy"]: float(d.get("eq") or 0) for d in bal.get("details", [])}
                    for inst_id, rec in list(self.st.get("positions", {}).items()):
                        if not inst_id.endswith("-USDT") or "-SWAP" in inst_id:
                            continue
                        base = inst_id.split("-")[0]
                        if held.get(base, 0) <= 0:
                            self.st["positions"].pop(inst_id, None)
                        else:
                            positions.append({"instId": inst_id, "pos": str(held[base]), "posSide": "net",
                                              "avgPx": str(rec.get("entry", 0)), "upl": "0", "margin": "0", "lever": "1"})
        except Exception as e:
            self.err(f"account/positions reconcile failed: {e}")
            return self.finish()
        # ---- reconcile: positions the exchange closed (SL/TP hit)
        live_ids = {p["instId"] for p in positions}
        for inst_id in list(st["positions"]):
            if self.live and inst_id not in live_ids:
                rec = st["positions"].pop(inst_id)
                pnl, px = "", ""
                h = []
                try:
                    h = self.x.positions_history(inst_id, 5)
                    if h:
                        pnl, px = h[0].get("realizedPnl", ""), h[0].get("closeAvgPx", "")
                        st["realized_pnl"] = float(st.get("realized_pnl", 0.0)) + float(pnl or 0)
                except OKXError as e:
                    self.err(f"positions-history {inst_id}: {e}")
                self.cancel_algos(inst_id)
                log_trade({"time": now_local().isoformat(timespec="seconds"), "event": "closed",
                           "instId": inst_id, "side": rec["side"], "price": px, "pnl": pnl,
                           "reason": "stop/tp/exchange", "mode": self.mode})
                self.ev("position closed on exchange", instId=inst_id, pnl=pnl)
                hh = h[0] if h else {}
                J.record("close", instId=inst_id, trade_id=rec.get("trade_id"), source="exchange (positions-history)",
                         exit=hh.get("closeAvgPx"), realized_pnl=hh.get("realizedPnl"), pnl=hh.get("pnl"),
                         fee=hh.get("fee"), funding=hh.get("fundingFee"), close_type=hh.get("type"),
                         close_time_ms=int(hh.get("uTime") or 0) or None, last_stop=rec.get("stop"), tp=rec.get("tp"),
                         init_stop=rec.get("init_stop"))

        self.log["equity"] = equity
        if st.get("start_equity") is None:
            st["start_equity"] = equity
        today = now_local().date().isoformat()
        if st.get("day") != today or not st.get("day_start_equity"):
            st["day"], st["day_start_equity"], st["day_locked"] = today, equity, False
        elif equity > 0 and abs(equity / st["day_start_equity"] - 1) > 0.5:
            st["day_start_equity"] = equity
            self.ev("reset day_start_equity after large equity jump", day_start=round(equity, 2))
        if not st.get("start_equity"):
            st["start_equity"] = equity
        if not st.get("peak_equity"):
            st["peak_equity"] = equity
        can_open, must_flatten, why = guardrail_status(st, equity)
        self.ev("guardrails", equity=round(equity, 2), peak=round(st["peak_equity"], 2),
                day_start=round(st["day_start_equity"], 2), can_open=can_open, status=why)
        if st.get("halted"):
            open(HALT_F, "w").write(st.get("halt_reason", "") + "\n")

        # ---- flatten if guardrail says so
        if must_flatten and positions:
            for p in positions:
                self.close(p["instId"], p.get("posSide"), "guardrail: " + why)
            positions = []

        # ---- manage open positions
        for p in positions:
            inst_id = p["instId"]
            try:
                f = self.features(inst_id)
                last = f.iloc[-1]
                pos_amt = float(p["pos"])
                side = 1 if (p.get("posSide") == "long" or (p.get("posSide") == "net" and pos_amt > 0)) else -1
                rec = st["positions"].get(inst_id)
                if rec is None:  # position not opened by bot: adopt with a strategy stop
                    entry = float(p["avgPx"])
                    stop, tp = initial_stop_tp(side, entry, last["atr"])
                    stop = stop or (entry * (1 - config.MAX_STOP_PCT * side))
                    rec = {"side": side, "entry": entry, "init_stop": stop, "stop": stop, "tp": tp,
                           "entry_ts": int(p.get("cTime") or 0), "adopted": True}
                    st["positions"][inst_id] = rec
                    self.ev("adopted unknown position", instId=inst_id)
                if should_exit_on_trend(side, last):
                    self.close(inst_id, p.get("posSide"), "4h trend flipped against position")
                    continue
                since = f[f["ts"] >= rec["entry_ts"] - 3600_000]
                hi = max(rec["entry"], since["h"].max() if len(since) else rec["entry"])
                lo = min(rec["entry"], since["l"].min() if len(since) else rec["entry"])
                new_stop = update_stop(side, rec["entry"], rec["init_stop"], rec["stop"], hi, lo, last["atr"])
                exit_side = "sell" if side == 1 else "buy"
                expected_ps = p.get("posSide") or "net"
                algos = [a for a in self.sl_algos(inst_id)
                         if a.get("side", exit_side) == exit_side
                         and a.get("posSide", expected_ps) == expected_ps]
                algo = algos[0] if algos else None
                # hygiene: drop redundant smaller stops when the largest one already covers the whole position
                if algo and len(algos) > 1 and float(algo.get("sz") or 0) >= abs(pos_amt):
                    try:
                        self.x.cancel_algos([{"algoId": a["algoId"], "instId": inst_id} for a in algos[1:]])
                        self.ev("removed redundant stop orders", instId=inst_id, n=len(algos) - 1)
                    except OKXError as e:
                        self.err(f"cancel redundant SL {inst_id}: {e}")
                rec["contracts"] = abs(pos_amt)
                # Fetch only this held instrument; risk management must not depend on the universe.
                self.instr[inst_id] = self.x._req(
                    "GET", "/api/v5/public/instruments", {"instType": self.inst_type, "instId": inst_id},
                    demo_public=bool(self.live and config.DEMO))[0]
                tick = self.instr[inst_id]["tickSz"]
                ps = p.get("posSide") if p.get("posSide") != "net" else None
                # Live demo last/mark (from /account/positions) — never use a stale candle close for SL validity.
                live_last, live_mark = live_sl_refs(p)
                if live_last is None:
                    live_last = float(self.x._req(
                        "GET", "/api/v5/market/ticker", {"instId": inst_id})[0]["last"])
                live_ref = live_mark or live_last or float(last["c"])
                # trail level computed above; never loosen vs the live exchange stop except via the liq nudge
                if algo is not None:
                    ex_stop = float(algo["slTriggerPx"])
                    new_stop = max(new_stop, ex_stop) if side == 1 else min(new_stop, ex_stop)
                nudged = None
                if getattr(config, "LIQMAP_NUDGE_OPEN", False) and inst_id.endswith("-SWAP"):
                    nudged = self.liq_nudge_open(inst_id, side, rec, new_stop, live_ref, float(last["atr"]),
                                                 last_px=live_last, mark_px=live_mark)
                    if nudged is not None:
                        new_stop = nudged["new_stop"]
                # Clamp so shorts stay above last/mark and longs stay below (OKX 51278/51302).
                new_stop, sl_clamped, sl_reason = clamp_sl_trigger(side, new_stop, live_last, live_mark)
                if sl_clamped:
                    self.ev("SL clamped to live last/mark", instId=inst_id, reason=sl_reason,
                            last=live_last, mark=live_mark)
                algos = self.ensure_sl_coverage(inst_id, side, abs(pos_amt), new_stop, ps,
                                               self.instr[inst_id]["lotSz"])
                if algos is None:
                    continue
                algo = algos[0] if algos else None
                if algo is not None:
                    ex_stop = float(algo["slTriggerPx"])
                    tighter = (side == 1 and new_stop > ex_stop * 1.0005) or (side == -1 and new_stop < ex_stop * 0.9995)
                    moved = abs(new_stop / ex_stop - 1) > 0.0005
                    if tighter or (nudged is not None and moved):
                        if not tighter and nudged is not None:
                            # Nudge wanted a move but clamp removed the tighten — skip soft.
                            self.ev("stop amend skipped (old SL kept)", instId=inst_id,
                                    reason="proposed stop not tighter after live last/mark clamp",
                                    proposed=new_stop, exchange_stop=ex_stop, last=live_last, mark=live_mark)
                            new_stop = ex_stop
                        else:
                            ok, new_stop = self.replace_sl(inst_id, side, abs(pos_amt), algo, new_stop, ps, tick,
                                                           last_px=live_last, mark_px=live_mark)
                            if not ok:
                                new_stop = ex_stop   # keep what is actually on the exchange
                            elif nudged is not None and not sl_clamped:
                                adj = {**nudged, "instId": inst_id, "exchange_old": ex_stop,
                                       "exchange_new": float(fmt_px(new_stop, tick))}
                                J.record("stop", instId=inst_id, trade_id=rec.get("trade_id"), old=ex_stop,
                                         new=adj["exchange_new"], kind="liq_nudge",
                                         reason=f"est. liq cluster(s) {nudged['clusters']}; max loss {nudged['risk_ratio']}x planned")
                                self.log.setdefault("stop_adjustments", []).append(adj)
                                log_trade({"time": now_local().isoformat(timespec="seconds"), "event": "stop_nudge",
                                           "instId": inst_id, "side": "long" if side == 1 else "short",
                                           "contracts": abs(pos_amt), "price": live_ref, "stop": fmt_px(new_stop, tick),
                                           "reason": f"liq cluster; old {ex_stop}; max loss {nudged['risk_ratio']}x planned",
                                           "mode": self.mode})
                            else:
                                kind = "trail_clamped" if sl_clamped else ("liq_nudge" if nudged is not None else "trail")
                                self.ev("trailed stop", instId=inst_id, old=algo["slTriggerPx"],
                                        new=fmt_px(new_stop, tick), kind=kind, last=live_last, mark=live_mark)
                                J.record("stop", instId=inst_id, trade_id=rec.get("trade_id"), old=ex_stop,
                                         new=float(fmt_px(new_stop, tick)), kind=kind,
                                         reason=(sl_reason if sl_clamped else
                                                 (f"est. liq cluster(s) {nudged['clusters']}; max loss {nudged['risk_ratio']}x planned"
                                                  if nudged is not None else
                                                  f"chandelier trail {config.TRAIL_ATR_MULT}xATR after +{config.TRAIL_START_R}R")))
                                if nudged is not None:
                                    adj = {**(nudged or {}), "instId": inst_id, "exchange_old": ex_stop,
                                           "exchange_new": float(fmt_px(new_stop, tick)), "clamped": sl_clamped}
                                    self.log.setdefault("stop_adjustments", []).append(adj)
                rec["stop"] = new_stop
                self.ev("holding", instId=inst_id, side=side, upl=p.get("upl"), stop=round(new_stop, 6))
            except Exception as e:
                self.err(f"manage {inst_id}: {e}")

        # Universe availability gates every kind of new entry, never position management.
        universe, universe_failed = [], False
        try:
            universe = self.universe(self.inst_type)
        except Exception as e:
            universe_failed = True
            self.err(f"universe fetch failed: {e}")
        if universe_failed:
            can_open = False
        if getattr(self, "protection_failed", False):
            can_open = False

        # ---- spot: allocation sleeves (BTC/ETH/OKB hold-or-USDT) + manage spot longs. Runs before swap
        #      entries so USDT freed by sleeve sells is available as swap margin this same cycle.
        # ---- news + macro-event + funding risk inputs (computed before any new entry, spot or swap)
        syms = [u.split("-")[0] for u in universe]
        try:
            ns = news.sentiment(syms)
        except Exception as e:
            ns = {"market": 0.0, "coins": {}, "n": 0, "errors": [str(e)], "high_impact": []}
        hi_news = ns.get("high_impact") or []
        self.log["news"] = {"market": ns["market"], "coins": ns["coins"], "n": ns["n"], "errors": ns["errors"],
                            "high_impact": hi_news}
        self.ev("news sentiment", market=ns["market"], n_items=ns["n"], feed_errors=len(ns["errors"]),
                high_impact=len(hi_news))
        self.block_longs = bool(hi_news)
        if hi_news:
            self.ev("NEW LONGS BLOCKED: high-impact negative news", items=[h["title"] for h in hi_news])
        try:
            ev_mode, ev = macro_events.risk_window()
        except Exception as e:
            ev_mode, ev = None, None
            self.err(f"event calendar: {e}")
        self.size_mult = 1.0
        if ev_mode == "block":
            can_open, why = False, "macro event window: " + macro_events.fmt(ev)
            self.ev("NEW ENTRIES BLOCKED: high-impact macro event", event=macro_events.fmt(ev))
        elif ev_mode == "halve":
            self.size_mult = 0.5
            self.ev("new position size halved: macro event within %gh" % config.EVENT_HALVE_BEFORE_H,
                    event=macro_events.fmt(ev))
        self.log["event_window"] = {"mode": ev_mode, "event": macro_events.fmt(ev) if ev else None}
        self.funding = {}
        if config.FUNDING_FILTER:
            try:
                self.funding = funding.all_rates()
            except Exception as e:
                self.err(f"funding rates: {e}")
        if self.funding and getattr(config, "FUNDING_HARVEST_ENABLED", False):
            try:
                self.manage_funding_harvest()
            except Exception as e:
                self.err(f"funding harvest management: {e}")

        # ---- spot: allocation sleeves (BTC/ETH/OKB hold-or-USDT) + manage spot longs. Runs before swap
        #      entries so USDT freed by sleeve sells is usable as swap margin this same cycle.
        spot = None
        self.spot = None
        spot_ready = False
        if not self.swaps_blocked:
            spot = SpotManager(self)
            try:
                if not spot.recover_pending():
                    can_open = False
            except Exception as e:
                can_open = False
                self.err(f"spot order recovery: {e}")
            try:
                spot.manage_longs(must_flatten, why)
            except Exception as e:
                can_open = False
                self.err(f"spot position management: {e}")
            if getattr(self, "protection_failed", False):
                can_open = False
            try:
                spot.load_market()
                self.spot = spot
                spot_ready = True
            except Exception as e:
                can_open = False
                self.err(f"spot market: {e}")
            if spot_ready:
                try:
                    spot.run_alloc(can_rebuy=can_open and not self.block_longs)
                except Exception as e:
                    can_open = False
                    self.err(f"spot allocation: {e}")

        attempted_swaps = set()
        def spot_entries():
            if spot_ready and can_open and not self.block_longs and not getattr(self, "protection_failed", False):
                try:
                    held_now = {p["instId"] for p in positions} | set(st["positions"]) | attempted_swaps
                    spot.enter_longs(equity * self.size_mult, ns, held_now)
                except Exception as e:
                    self.err(f"spot longs: {e}")

        # ---- look for new swap entries
        # spot longs that were routed from ~1x swap signals count toward the swap position cap
        routed = [k for k, r in (st.get("spot", {}).get("longs") or {}).items() if r.get("routed_from")]
        slots = config.MAX_POSITIONS - len(positions) - len(routed)
        if not can_open or slots <= 0:
            self.ev("no new swap entries", reason=("universe fetch failed" if universe_failed else why) if not can_open else "max swap positions reached")
            spot_entries()
            return self.finish(equity, len(positions))
        cands, scanned = [], {}
        held = {p["instId"] for p in positions}
        spot_bases = {k.split("-")[0] for k in (st.get("spot", {}).get("longs") or {})}
        for inst_id in universe:
            if inst_id in held or inst_id.split("-")[0] in spot_bases:
                continue
            try:
                f = self.features(inst_id)
                time.sleep(0.05)
            except Exception as e:
                self.err(f"candles {inst_id}: {e}")
                continue
            if f is None:
                continue
            last = f.iloc[-1]
            sig = int(last["signal"])
            scanned[inst_id] = {"trend4h": int(last["trend4h"]), "signal": sig, "close": last["c"],
                                "rsi": round(float(last["rsi"]), 1)}
            if sig == 0:
                continue
            if st["last_signal_ts"].get(inst_id) == int(last["ts"]):
                continue   # already acted on this bar
            if sig == 1 and self.block_longs:
                self.ev("long blocked: high-impact news", instId=inst_id); continue
            fr = self.funding.get(inst_id)
            if config.FUNDING_FILTER and funding.entry_blocked(sig, fr):
                self.ev("entry blocked: would pay high funding", instId=inst_id, side=sig,
                        rate_8h_pct=round(fr["rate_8h"] * 100, 4)); continue
            sym = inst_id.split("-")[0]
            coin_s, mkt_s = ns["coins"].get(sym, 0.0), ns["market"]
            T = config.NEWS_BLOCK_THRESHOLD
            if sig == 1 and (coin_s < -T or mkt_s < -T):
                self.ev("long blocked by negative news", instId=inst_id, coin=coin_s, market=mkt_s); continue
            if sig == -1 and (coin_s > T or mkt_s > T):
                self.ev("short blocked by positive news", instId=inst_id, coin=coin_s, market=mkt_s); continue
            score = float(last["strength"]) * (1 + 0.5 * sig * (coin_s + 0.5 * mkt_s))
            try:
                self.ctx = getattr(self, "ctx", {})
                self.ctx[inst_id] = {
                    "signal_bar_ts": int(last["ts"]), "close": float(last["c"]),
                    "breakout_level": float(last["hh20"] if sig == 1 else last["ll20"]),
                    "donchian_lookback_h": config.DONCHIAN_N, "trend_tf": config.TREND_TF,
                    "trend": int(last["trend4h"]), "ema50": float(last["ema50"]), "ema200": float(last["ema200"]),
                    "atr": float(last["atr"]), "atr_pct": round(float(last["atr"]) / float(last["c"]) * 100, 3),
                    "rsi": round(float(last["rsi"]), 1), "strength": round(float(last["strength"]), 3),
                    "news_coin": coin_s, "news_market": mkt_s, "score": round(score, 3),
                    "funding": fr}
            except Exception:
                pass
            cands.append((score, inst_id, sig, float(last["atr"]), int(last["ts"])))
        self.log["scan"] = scanned
        cands.sort(reverse=True)
        self.ev("signals", candidates=[(c[1], c[2], round(c[0], 2)) for c in cands])
        used_margin = sum(float(p.get("margin") or p.get("imr") or 0) for p in positions)
        for score, inst_id, sig, atr_v, bar_ts in cands[:slots]:
            if getattr(self, "protection_failed", False) or (st.get("spot", {}).get("pending_orders") or {}):
                break
            attempted_swaps.add(inst_id)
            self.enter(inst_id, sig, atr_v, bar_ts, equity, used_margin)
        spot_entries()
        return self.finish(equity, len(positions))

    def enter(self, inst_id, side, atr_v, bar_ts, equity, used_margin):
        st, ins = self.st, self.instr[inst_id]
        px = self.last_px[inst_id]
        stop, tp = initial_stop_tp(side, px, atr_v)
        if stop is None:
            self.ev("skip: stop too wide", instId=inst_id); return
        liq_near, liq_info = None, None
        if getattr(config, "LIQMAP_STOP_NUDGE", False) and inst_id.endswith("-SWAP"):
            # Reference only: move the stop beyond a dense ESTIMATED liquidation cluster sitting near it
            # (<= MAX_STOP_PCT). Sizing below uses the new stop, so risk per trade is unchanged; TP unchanged.
            try:
                lv, _ = liqmap.current_levels(self.x, inst_id)
                liq_near = [(("long-liq" if s == 1 else "short-liq"), round(lo, 8), round(hi, 8), round(u), round(d, 2))
                            for s, lo, hi, u, d in liqmap.nearest_clusters(lv, px, span=0.05, top=4)]
                new_stop, info = liqmap.adjust_stop(side, px, stop, atr_v, lv)
                liq_info = info
                if info:
                    self.ev("liq-cluster near stop", instId=inst_id, **info)
                    self.log.setdefault("liq_nudges", []).append({"instId": inst_id, **info})
                stop = new_stop
            except Exception as e:
                self.err(f"liqmap {inst_id}: {e} (stop not adjusted)")
        tick = ins["tickSz"]
        lot, min_sz = float(ins["lotSz"]), float(ins["minSz"])
        is_spot = getattr(self, "inst_type", "SWAP") == "SPOT" or self.swaps_blocked
        if is_spot and side != 1:
            self.ev("skip: spot mode is long-only", instId=inst_id); return
        intent, fr_now = "trend", {}
        tf_mult = tradfi.size_mult(inst_id)   # 1.0 for crypto; 0.5 (x0.5 off-session/weekend) for stock/commodity perps
        notional, lev, margin = size_position(equity * getattr(self, "size_mult", 1.0) * tf_mult, px, stop)
        plan_notional0, plan_risk0 = notional, notional * abs(px - stop) / px   # risk-based plan (pre-clamp)
        if is_spot:
            # OKX demo/spot market buys: size in quote (USDT) with tgtCcy=quote_ccy (base-sized market hits 51020).
            lev = 1
            notional = min(notional, equity * config.MAX_MARGIN_PCT, self.usdt_avail * 0.95)
            notional = max(notional, 10.0)  # exchange min order amount
            if notional > self.usdt_avail * 0.95:
                self.ev("skip: not enough available USDT", instId=inst_id); return
            contracts = round(notional / px, 8)  # informational; exchange sizes by quote
            margin = notional
            order = {"instId": inst_id, "tdMode": "cash", "side": "buy", "ordType": "market",
                     "sz": str(round(notional, 2)), "tgtCcy": "quote_ccy",
                     "clOrdId": "bot" + uuid.uuid4().hex[:20],
                     "attachAlgoOrds": [{"slTriggerPx": fmt_px(stop, tick), "slOrdPx": "-1", "slTriggerPxType": "last",
                                         "tpTriggerPx": fmt_px(tp, tick), "tpOrdPx": "-1", "tpTriggerPxType": "last"}]}
            pos_side = None
        else:
            spot_id = inst_id.replace("-SWAP", "")
            sp = getattr(self, "spot", None)
            fr_now = (getattr(self, "funding", {}) or {}).get(inst_id) or {}
            if getattr(config, "SPOT_PREFER_FOR_LOW_LEV_LONGS", False) and side == 1 and lev <= getattr(
                    config, "SPOT_PREFER_MAX_LEV", 1) and getattr(config, "FUNDING_HARVEST_ENABLED", False) \
                    and sizing.funding_harvest(fr_now.get("rate_8h"), None, config.FUNDING_HARVEST_MIN_8H):
                ls = funding.last_settled(inst_id)
                if sizing.funding_harvest(fr_now.get("rate_8h"), ls, config.FUNDING_HARVEST_MIN_8H):
                    intent = "funding_harvest"
                    self.ev("1x long kept as SWAP: funding harvest (longs receive)", instId=inst_id,
                            rate_8h_pct=round(fr_now["rate_8h"] * 100, 4), last_settled_pct=round((ls or 0) * 100, 4))
            if intent != "funding_harvest" and getattr(config, "SPOT_PREFER_FOR_LOW_LEV_LONGS", False) and sizing.prefer_spot(
                    side, lev, sp is not None and spot_id in sp.inst
                    and spot_id.split("-")[0] not in config.SPOT_ALLOC_COINS, tradfi.is_tradfi(inst_id),
                    getattr(config, "SPOT_PREFER_MAX_LEV", 1)):
                self.ev("long routed to spot (planned swap leverage %dx)" % lev, instId=inst_id, spot=spot_id)
                ok = sp.open_long(spot_id, atr_v, bar_ts, equity * getattr(self, "size_mult", 1.0) * tf_mult,
                                  routed_from=inst_id, extra_rat={"planned_swap_lev": lev})
                if ok != "fallback":
                    st["last_signal_ts"][inst_id] = bar_ts
                    return ok
                intent = "spot_too_thin"
            if getattr(config, "LIQ_FILTER_ENABLED", False) and self.live:
                notional, ok_liq, liq = self.liquidity_check(inst_id, side, px, stop, notional,
                                                             float(ins["ctVal"]) * float(ins.get("ctMult") or 1))
                if not ok_liq:
                    st["last_signal_ts"][inst_id] = bar_ts
                    return
                lev = max(1, min(lev, math.ceil(notional / (equity * config.MAX_MARGIN_PCT) - 1e-9)))
            ct_val = float(ins["ctVal"])
            contracts = round_step(notional / (ct_val * px), lot)
            if contracts < min_sz:
                self.ev("skip: below min size", instId=inst_id, contracts=contracts, min=min_sz); return
            notional = contracts * ct_val * px
            margin = notional / lev
            if margin > self.usdt_avail * 0.95:
                # Futures mode: margin must come from USDT. Shrink the position to fit (raise lev up to max first).
                lev = config.MAX_LEVERAGE
                max_notional = self.usdt_avail * 0.95 * lev
                contracts = round_step(min(notional, max_notional) / (ct_val * px), lot)
                if contracts < min_sz:
                    self.ev("skip: not enough available USDT", instId=inst_id, usdt_avail=round(self.usdt_avail, 2)); return
                notional = contracts * ct_val * px
                margin = notional / lev
                self.ev("size reduced to fit available USDT margin", instId=inst_id, notional=round(notional, 2), lev=lev)
            if margin > equity * config.MAX_MARGIN_PCT + 1e-9 or lev > config.MAX_LEVERAGE:
                self.err(f"sizing guard tripped for {inst_id}"); return
            pos_side = self.pos_side(side)
            order = {"instId": inst_id, "tdMode": config.TD_MODE, "side": "buy" if side == 1 else "sell",
                     "ordType": "market", "sz": str(contracts), "clOrdId": "bot" + uuid.uuid4().hex[:20],
                     "attachAlgoOrds": [{"slTriggerPx": fmt_px(stop, tick), "slOrdPx": "-1", "slTriggerPxType": "mark",
                                         "tpTriggerPx": fmt_px(tp, tick), "tpOrdPx": "-1", "tpTriggerPxType": "last"}]}
            if pos_side:
                order["posSide"] = pos_side
        plan = {"instId": inst_id, "side": "long" if side == 1 else "short", "intent": intent, "price": px, "contracts": contracts,
                "notional": round(notional, 2), "leverage": lev, "margin": round(margin, 2),
                "stop": float(fmt_px(stop, tick)), "tp": float(fmt_px(tp, tick)),
                "risk_usdt": round(abs(px - stop) / px * notional, 2)}
        if not self.live:
            self.ev("DRY-RUN would open", **plan, order=order)
            st["last_signal_ts"][inst_id] = bar_ts
            return
        try:
            if not (getattr(self, "inst_type", "SWAP") == "SPOT" or self.swaps_blocked):
                self.x.set_leverage(inst_id, lev, pos_side)
                # Affordability + tier caps at the leverage just set (avoids 51004 tier cap / 51008 margin).
                exch_max = None
                try:
                    ms = self.x.max_size(inst_id, config.TD_MODE)[0]
                    exch_max = float(ms.get("maxBuy" if side == 1 else "maxSell") or 0)
                except OKXError as e:
                    self.err(f"max-size {inst_id}: {e} (clamping by balance only)")
                c2, notes = sizing.clamp_swap(contracts, lot, min_sz, lev, float(ins["ctVal"]), px, self.usdt_avail,
                                              exch_max, float(ins.get("maxMktSz") or 0) or None,
                                              getattr(config, "ORDER_SIZE_BUFFER", 0.95))
                if notes:
                    self.ev("swap order size check", instId=inst_id, planned_ct=contracts, exch_max_ct=exch_max, notes=notes)
                if c2 <= 0:
                    self.ev("skip: not affordable / below min after limits", instId=inst_id)
                    st["last_signal_ts"][inst_id] = bar_ts; return
                if c2 != contracts:
                    contracts = c2
                    order["sz"] = str(contracts)
                    notional = contracts * float(ins["ctVal"]) * px
                    margin = notional / lev
                    plan.update(contracts=contracts, notional=round(notional, 2), margin=round(margin, 2),
                                risk_usdt=round(abs(px - stop) / px * notional, 2))
            if not (getattr(self, "inst_type", "SWAP") == "SPOT" or self.swaps_blocked):
                # HARD SANITY CHECK with a fresh contract spec from the executing venue (demo when DEMO).
                spec = self.x._req("GET", "/api/v5/public/instruments", {"instType": "SWAP", "instId": inst_id},
                                   demo_public=bool(config.DEMO))[0]
                ok_s, info = sizing.order_sanity(order["sz"], spec["ctVal"], spec.get("ctMult") or 1, px, stop,
                                                 plan_notional0, plan_risk0, getattr(config, "ORDER_SANITY_TOL", 1.2))
                if not ok_s or str(spec["ctVal"]) != str(ins["ctVal"]):
                    self.err(f"ORDER REFUSED {inst_id}: size sanity check failed {info} "
                             f"(ctVal used {ins['ctVal']}, venue ctVal {spec['ctVal']} ctMult {spec.get('ctMult')})")
                    self.log.setdefault("refused_orders", []).append({"instId": inst_id, **info, "ctVal_used": ins["ctVal"],
                                                                      "ctVal_venue": spec["ctVal"]})
                    st["last_signal_ts"][inst_id] = bar_ts
                    return
            res = self.x.place_order(order)[0]
            st["last_signal_ts"][inst_id] = bar_ts
            save_state(self.st)
            ord_id = res.get("ordId")
            o = {}
            for _ in range(16):   # wait for the market order to fully fill (was reading partial fills)
                time.sleep(0.5)
                o = self.x.get_order(inst_id, ord_id)[0]
                if o.get("state") in ("filled", "canceled", "mmp_canceled"):
                    break
            fill_px = float(o.get("avgPx") or px)
            filled = float(o.get("accFillSz") or 0)
            st["last_signal_ts"][inst_id] = bar_ts
            if filled <= 0:
                self.err(f"order {ord_id} on {inst_id} not filled (state={o.get('state')})"); return
            ctv = float(ins.get("ctVal") or 1)
            act_notional, partial = sizing.fill_summary(float(contracts), filled, fill_px, ct_val=ctv)
            if partial:
                pf = {"instId": inst_id, "ordId": ord_id, "requested_ct": contracts, "filled_ct": filled, "avg_px": fill_px,
                      "notional": act_notional, "state": o.get("state"), "cancelSource": o.get("cancelSource")}
                self.ev("PARTIAL FILL", **pf)
                self.log.setdefault("partial_fills", []).append(pf)
                J.record("partial_fill", trade_id=ord_id, **pf)
            plan.update(contracts=filled, notional=round(act_notional, 2), margin=round(act_notional / lev, 2),
                        risk_usdt=round(abs(fill_px - stop) * filled * ctv, 2), requested_contracts=contracts)
            margin = act_notional / lev
            st["positions"][inst_id] = {"side": side, "entry": fill_px, "init_stop": stop, "stop": stop, "tp": tp,
                                        "entry_ts": bar_ts + 3600_000, "contracts": filled, "lev": lev, "ordId": ord_id,
                                        "intent": intent}
            save_state(self.st)
            algo = None
            for _ in range(6):    # attached TP/SL can take a moment to appear
                time.sleep(0.5)
                algo = self.sl_algo(inst_id)
                if algo is not None:
                    break
            if self.ensure_sl_coverage(inst_id, side, filled, stop, pos_side, lot) is None:
                return
            self.usdt_avail = max(0.0, self.usdt_avail - margin)
            log_trade({"time": now_local().isoformat(timespec="seconds"), "event": "open", "instId": inst_id,
                       "side": plan["side"], "contracts": filled, "price": fill_px, "stop": plan["stop"],
                       "tp": plan["tp"], "leverage": lev, "notional": plan["notional"], "ordId": ord_id,
                       "reason": "breakout+trend" + (f" tradfi x{tf_mult:g}" if tf_mult != 1 else "")
                                 + (" funding_harvest" if intent == "funding_harvest" else ""), "mode": self.mode})
            self.ev("opened", **plan, ordId=ord_id, fill=fill_px)
            try:
                ctval = 1.0 if (getattr(self, "inst_type", "SWAP") == "SPOT" or self.swaps_blocked) else float(ins["ctVal"])
                ev_mode, ev_e = (self.log.get("event_window") or {}).get("mode"), (self.log.get("event_window") or {}).get("event")
                rat = {**(getattr(self, "ctx", {}).get(inst_id) or {}), "reconstructed": False,
                       "liq_clusters_near": liq_near, "liq_stop_nudge": liq_info,
                       "event_window": ev_mode, "event": ev_e,
                       "size_mult_event": getattr(self, "size_mult", 1.0), "size_mult_tradfi": tf_mult,
                       "session": tradfi.session() if tradfi.is_tradfi(inst_id) else None,
                       "plan_price": px, "planned_stop": plan["stop"], "planned_tp": plan["tp"], "intent": intent,
                       "funding_at_entry": fr_now if not is_spot else None}
                planned_risk = abs(fill_px - stop) * filled * ctval
                st["positions"][inst_id].update({"trade_id": ord_id, "rationale": rat,
                                                 "planned_risk_usdt": round(planned_risk, 4)})
                J.record("open", trade_id=ord_id, instId=inst_id, market=J.market_of(inst_id),
                         side="long" if side == 1 else "short", entry=fill_px, size=filled, ct_val=ctval,
                         notional=round(filled * ctval * fill_px, 2), lev=lev, init_stop=stop, tp=tp,
                         planned_risk_usdt=round(planned_risk, 4), rationale=rat, intent=intent)
            except Exception as e:
                self.err(f"journal open {inst_id}: {e}")
        except OKXError as e:
            self.err(f"entry {inst_id}: {e}")

    def liquidity_check(self, inst_id, side, px, stop, notional, coins_per_unit, market="swap"):
        """Demo order book: shrink size for expected stop slippage, then skip if entry/stop-side depth within
        LIQ_DEPTH_PCT of mid can't absorb it. Returns (notional, ok, info)."""
        base = inst_id.split("-")[0]
        floor = config.SLIP_FLOOR_MAJOR if base in config.SLIP_MAJORS else config.SLIP_FLOOR_OTHER
        try:
            bk = self.x._req("GET", "/api/v5/market/books", {"instId": inst_id, "sz": "400"}, demo_public=bool(config.DEMO))[0]
        except Exception as e:
            self.err(f"orderbook {inst_id}: {e} -> skipping entry (liquidity unknown)")
            return notional, False, None
        if not bk.get("asks") or not bk.get("bids"):
            info = {"reason": "empty book"}; ok = False; new = notional
        else:
            new, info, ok = sizing.liquidity_size(bk, side, px, stop, notional, coins_per_unit, floor,
                                                  config.LIQ_DEPTH_PCT, config.LIQ_MIN_COVER)
        info["slip_floor_pct"] = floor * 100
        self.log.setdefault("liquidity", []).append({"instId": inst_id, "ok": ok, **info})
        if not ok:
            self.ev("skipped: thin book", instId=inst_id, **info)
            J.record("skip", instId=inst_id, reason="thin book", market=market, side="long" if side == 1 else "short", **info)
        elif new < notional * 0.999:
            self.ev("size reduced for expected stop slippage", instId=inst_id, **info)
        return new, ok, info

    def manage_funding_harvest(self):
        """Held funding-harvest longs: count consecutive cycles with funding flipped positive; flag, and close
        only if persistently at/above the entry-block level. Stops are untouched (close() cancels algos)."""
        for inst_id, rec in list(self.st.get("positions", {}).items()):
            if rec.get("intent") != "funding_harvest":
                continue
            r8 = ((self.funding or {}).get(inst_id) or {}).get("rate_8h")
            cnt, act = sizing.harvest_flip_action(r8, rec.get("fund_flip_count", 0), config.FUNDING_HARVEST_FLIP_8H,
                                                  config.FUNDING_HARVEST_FLIP_CYCLES, config.FUNDING_HARVEST_CLOSE_8H)
            rec["fund_flip_count"] = cnt
            info = dict(instId=inst_id, rate_8h_pct=round((r8 or 0) * 100, 4), cycles_positive=cnt)
            if act == "flag":
                self.ev("FUNDING HARVEST FLAG: funding flipped positive persistently (holding; stop kept)", **info)
                self.log.setdefault("funding_harvest_flags", []).append(info)
            elif act == "close":
                self.ev("FUNDING HARVEST EXIT: funding persistently >= entry-block level", **info)
                self.log.setdefault("funding_harvest_flags", []).append({**info, "action": "close"})
                self.close(inst_id, self.pos_side(1), "funding_flip")

    def finish(self, equity=None, n_pos=0):
        save_state(self.st)
        if equity is not None:
            log_equity(equity, self.mode, n_pos)
        with open(DECISIONS_F, "a") as f:
            f.write(json.dumps(self.log, default=str, ensure_ascii=False) + "\n")
        return self.log
