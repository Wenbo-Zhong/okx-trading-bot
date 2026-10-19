"""mm_glitch band trading: fade the edges of a confirmed market-maker-malfunction oscillation band.

The planner supplies band edges after a confirmed regime; this module validates them and runs a
small state machine. Rules (config.RANGE_*):
  * long limit at lower + zone*W, short limit at upper - zone*W (W = upper - lower); first fill decides
    the first direction, then directions alternate after each take-profit.
  * TP inside the opposite edge by RANGE_TP_BUFFER*W; exchange-side SL outside the entry edge by
    RANGE_STOP_BUFFER*W (attached to the entry order). Stop distance <= 60% of the 20x liq distance.
  * size: first trade = risk RISK_PER_TRADE of equity; later trades = MARGIN of 1/3 cumulative realized
    regime profit at the regime leverage, still capped by RISK_PER_TRADE risk and MAX_MARGIN_PCT. If
    that is below the minimum order size (or profit <= 0) the strategy stops.
  * any stop-out, band break, regime end/cap, 30 min strategy cap, 10 min per-trade cap -> cancel all,
    flatten, stop. Local position state is cleared only after the exchange confirms the position is flat;
    the exchange stop is never cancelled while a position may still be open. Caps are enforced even when
    the price feed is down.
  * execute mode: cumulative profit (follow-on sizing) comes only from exchange realized PnL
    (positions-history realizedPnl = fills + fees + funding), never from an estimated exit price.
  * post-fee gate (edge()): a band is rejected unless its expected net edge per trade, after actual
    fees, spread/slippage and the fill-history win rate, is positive.
DEMO only. Dry-run (log-only) unless config.RANGE_TRADE_ENABLED and --execute."""
import json, math, time, uuid
from pathlib import Path
from okxbot import config

JOURNAL = lambda: Path(config.DATA_DIR, 'decision_journal.jsonl')


def journal(rec):
    rec = dict(rec, ts=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), kind='range_trade')
    with open(JOURNAL(), 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + '\n')


def levels(lower, upper, lev):
    w = upper - lower
    L = {'w': w, 'long_entry': lower + config.RANGE_ENTRY_ZONE * w, 'short_entry': upper - config.RANGE_ENTRY_ZONE * w,
         'long_tp': upper - config.RANGE_TP_BUFFER * w, 'short_tp': lower + config.RANGE_TP_BUFFER * w,
         'long_sl': lower - config.RANGE_STOP_BUFFER * w, 'short_sl': upper + config.RANGE_STOP_BUFFER * w}
    L['max_stop_frac'] = max((L['long_entry'] - L['long_sl']) / L['long_entry'],
                             (L['short_sl'] - L['short_entry']) / L['short_entry'])
    return L


def fill_history(path=None):
    """Executed range trades with exchange-confirmed realized PnL (dry-run / estimates excluded)."""
    rows = []
    try:
        for line in Path(path or JOURNAL()).read_text(encoding='utf-8').splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (r.get('kind') == 'range_trade' and r.get('event') == 'trade_closed' and r.get('mode') == 'execute'
                    and isinstance(r.get('realized_pnl'), (int, float))):
                rows.append(r)
    except OSError:
        pass
    return rows


def edge(lower, upper, fee_taker=None, spread_frac=None, history=None):
    """Expected net edge per trade (fraction of entry price) after costs. Returns a dict; 'ok' is True only
    when the expected net edge is > RANGE_MIN_NET_EDGE.
      fee_taker  : actual taker fee rate (positive cost); entries can fill aggressively and TP/SL exit as
                   market orders, so taker is assumed on both legs (conservative). Default config.
      spread_frac: current (ask-bid)/mid of the executing book; each leg pays max(half-spread, slip floor,
                   observed avg slippage).
      history    : executed trade rows (fill_history()); win rate = Beta-shrunk toward RANGE_PRIOR_WIN_RATE."""
    fee = config.SWAP_TAKER_FEE if fee_taker is None else max(0.0, float(fee_taker))
    hist = fill_history() if history is None else history
    slips = [abs(float(h['slip_frac'])) for h in hist if isinstance(h.get('slip_frac'), (int, float))]
    obs_slip = sum(slips) / len(slips) if slips else 0.0
    slip = max(config.RANGE_SLIP_FRAC, (spread_frac or 0.0) / 2, obs_slip)
    cost = 2 * fee + 2 * slip
    w = upper - lower
    entry = lower + config.RANGE_ENTRY_ZONE * w
    g = (1 - config.RANGE_ENTRY_ZONE - config.RANGE_TP_BUFFER) * w / entry      # gross at TP
    l = (config.RANGE_ENTRY_ZONE + config.RANGE_STOP_BUFFER) * w / entry       # gross loss at SL
    n, wins, k = len(hist), sum(1 for h in hist if h['realized_pnl'] > 0), config.RANGE_PRIOR_WEIGHT
    p = (wins + config.RANGE_PRIOR_WIN_RATE * k) / (n + k)
    net_win, net_loss = g - cost, l + cost
    ev = p * net_win - (1 - p) * net_loss
    be = net_loss / (net_win + net_loss) if net_win > 0 else 1.0
    return {'ok': net_win > 0 and ev > config.RANGE_MIN_NET_EDGE, 'ev_frac': ev, 'win_rate': p, 'breakeven_win_rate': be,
            'net_win_frac': net_win, 'net_loss_frac': net_loss, 'cost_frac': cost, 'fee_taker': fee, 'slip_frac': slip,
            'history_trades': n}


def validate(inst, lower, upper, regime_id, lev, now_s=None, state=None, costs=None):
    """Raise ValueError unless band + regime satisfy every mm_glitch rule."""
    from okxbot.plan import mm_liq_distance, mm_used_regimes
    if inst not in config.MM_GLITCH_INSTS:
        raise ValueError('only BTC/ETH/SOL USDT swaps')
    if config.TD_MODE != 'isolated':
        raise ValueError('isolated margin required')
    if type(lev) is not int or not 1 <= lev <= config.MM_GLITCH_MAX_LEVERAGE:
        raise ValueError('leverage exceeds MM_GLITCH_MAX_LEVERAGE')
    if not (0 < lower < upper) or any(map(math.isnan, (lower, upper))):
        raise ValueError('need 0 < lower < upper')
    mid = (lower + upper) / 2
    if (upper - lower) / mid * 1e4 < config.RANGE_MIN_BAND_BP:
        raise ValueError('band narrower than RANGE_MIN_BAND_BP')
    L = levels(lower, upper, lev)
    E = edge(lower, upper, **(costs or {}))
    L['edge'] = E
    if not E['ok']:
        raise ValueError('post-fee edge not positive: EV %.4f%%/trade at win rate %.0f%% (breakeven %.0f%%, cost %.3f%%)'
                         % (E['ev_frac'] * 100, E['win_rate'] * 100, E['breakeven_win_rate'] * 100, E['cost_frac'] * 100))
    if L['max_stop_frac'] > config.MM_GLITCH_STOP_LIQ_FRAC * mm_liq_distance(lev) + 1e-12:
        raise ValueError('stop distance exceeds 60% of liquidation distance')
    if state is None:
        try:
            state = json.loads(Path(config.DATA_DIR, 'mm_glitch_state.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ValueError('no mm_glitch detector state')
    reg = (state.get('regimes') or {}).get(inst)
    now_s = time.time() if now_s is None else now_s
    if not reg or reg.get('id') != regime_id or now_s - float(reg.get('start_s') or 0) > config.MM_GLITCH_REGIME_MAX_AGE_S:
        raise ValueError('regime not active for this instrument')
    if reg.get('book_ok') is not True:
        raise ValueError('book depth check failed')
    if regime_id in mm_used_regimes():
        raise ValueError('regime already used')
    return L


def size_qty(equity, entry, stop, lev, cum_profit, first, units, lot, min_sz):
    """Contracts for the next entry (0 -> stop strategy)."""
    risk_cap = equity * config.RISK_PER_TRADE
    dist = abs(entry - stop) + entry * config.RANGE_COST_FRAC   # loss at stop incl. fees+slippage
    q_risk = risk_cap / dist / units
    q_margin_cap = equity * config.MAX_MARGIN_PCT * lev / entry / units
    if first:
        q = min(q_risk, q_margin_cap)
    else:
        if cum_profit <= 0:
            return 0.0
        margin = cum_profit * config.RANGE_PROFIT_MARGIN_FRAC
        q = min(margin * lev / entry / units, q_risk, q_margin_cap)
    q = math.floor(q / lot + 1e-9) * lot
    return round(q, 8) if q >= min_sz else 0.0


def replay(bars, lower, upper, lev=20, equity=90936.0, fee=0.0005, slip=0.0005, units=1.0, lot=0.001, min_sz=0.001,
           start_idx=0, max_s=None):
    """Bar-level backtest with conservative intrabar ordering:
       - a bar that touches both an entry zone and its stop: entry then stop (loss);
       - TP is never credited in the bar the entry fills (path unknown);
       - a bar touching both TP and SL of an open trade: SL first;
       - if both entry zones are touched in one bar, the one nearer the bar open fills.
    bars: dicts ts,o,h,l,c (oldest first) starting at the first tradable bar."""
    L = levels(lower, upper, lev)
    max_s = max_s or config.RANGE_MAX_STRATEGY_S
    trades, pos, cum, want, eq_curve, why = [], None, 0.0, None, [0.0], None
    t0 = bars[start_idx]['ts']
    for b in bars[start_idx:]:
        if (b['ts'] - t0) / 1000 >= max_s:
            why = 'strategy 30 min cap'
            break
        if pos is None:
            if b['o'] <= L['long_sl'] or b['o'] >= L['short_sl']:
                why = 'band break while flat'; break
            hit_l = b['l'] <= L['long_entry'] and want in (None, 1)
            hit_s = b['h'] >= L['short_entry'] and want in (None, -1)
            if hit_l and hit_s:
                hit_l = abs(b['o'] - L['long_entry']) <= abs(b['o'] - L['short_entry']); hit_s = not hit_l
            if not (hit_l or hit_s):
                if b['l'] <= L['long_sl'] or b['h'] >= L['short_sl']:
                    why = 'band break while flat'; break
                continue
            side = 1 if hit_l else -1
            px = L['long_entry'] if side == 1 else L['short_entry']
            px = min(px, b['o']) if side == 1 else max(px, b['o'])      # gap through limit fills at open
            sl, tp = (L['long_sl'], L['long_tp']) if side == 1 else (L['short_sl'], L['short_tp'])
            q = size_qty(equity, px, sl, lev, cum, not trades, units, lot, min_sz)
            if q <= 0:
                why = 'next size below minimum'; break
            fill = px * (1 + side * slip)
            pos = {'side': side, 'entry': fill, 'qty': q, 'sl': sl, 'tp': tp, 'ts': b['ts'], 'margin': fill * q * units / lev}
            if (side == 1 and b['l'] <= sl) or (side == -1 and b['h'] >= sl):
                pass   # fall through to stop handling below (same bar, conservative)
            else:
                continue
        side = pos['side']
        sl_hit = b['l'] <= pos['sl'] if side == 1 else b['h'] >= pos['sl']
        tp_hit = (b['ts'] > pos['ts']) and (b['h'] >= pos['tp'] if side == 1 else b['l'] <= pos['tp'])
        timeout = (b['ts'] - pos['ts']) / 1000 >= config.RANGE_MAX_TRADE_S
        if sl_hit:
            x, reason = pos['sl'] * (1 - side * slip), 'stop'
        elif tp_hit:
            x, reason = pos['tp'] * (1 - side * slip), 'tp'
        elif timeout:
            x, reason = b['c'] * (1 - side * slip), 'trade 10 min cap'
        else:
            continue
        gross = (x - pos['entry']) * side * pos['qty'] * units
        fees = fee * pos['qty'] * units * (pos['entry'] + x)
        net = gross - fees
        cum += net
        trades.append({'side': 'long' if side == 1 else 'short', 'entry_ts': pos['ts'], 'exit_ts': b['ts'],
                       'entry': round(pos['entry'], 2), 'exit': round(x, 2), 'qty': pos['qty'],
                       'notional': round(pos['entry'] * pos['qty'] * units, 2), 'margin': round(pos['margin'], 2),
                       'gross': round(gross, 2), 'fees': round(fees, 2), 'net': round(net, 2), 'reason': reason})
        eq_curve.append(cum)
        pos = None
        if reason != 'tp':
            why = reason; break
        want = -side
    else:
        why = why or 'data end'
    if pos is not None:   # forced flatten at last bar close
        b = bars[-1] if why == 'data end' else b
        side = pos['side']; x = b['c'] * (1 - side * slip)
        gross = (x - pos['entry']) * side * pos['qty'] * units
        fees = fee * pos['qty'] * units * (pos['entry'] + x)
        cum += gross - fees; eq_curve.append(cum)
        trades.append({'side': 'long' if side == 1 else 'short', 'entry_ts': pos['ts'], 'exit_ts': b['ts'],
                       'entry': round(pos['entry'], 2), 'exit': round(x, 2), 'qty': pos['qty'], 'net': round(gross - fees, 2),
                       'gross': round(gross, 2), 'fees': round(fees, 2), 'reason': 'flatten: ' + why})
    peak, mdd = 0.0, 0.0
    for v in eq_curve:
        peak = max(peak, v); mdd = min(mdd, v - peak)
    return {'levels': {k: round(v, 4) for k, v in L.items()}, 'trades': trades, 'total_net': round(cum, 2),
            'max_drawdown': round(mdd, 2), 'stop_reason': why}


class Runner:
    """Live/dry-run executor. Uses only exchange-side orders: resting limit entries with attached
    TP + SL. `client` is an OKX client (DEMO); `price()` returns the latest WS price."""

    def __init__(self, client, inst, lower, upper, regime_id, lev=20, execute=False, clock=time.time,
                 price=None, regime_alive=None, spec=None, equity=None, sleep=time.sleep):
        self.c, self.inst, self.rid, self.lev = client, inst, regime_id, lev
        self.execute = bool(execute and config.RANGE_TRADE_ENABLED and config.DEMO)
        self.clock, self.price = clock, price
        self.regime_alive = regime_alive or self._regime_alive
        self.L = levels(lower, upper, lev)
        self.lower, self.upper = lower, upper
        self.spec = spec
        self.equity = equity
        self.start = clock()
        self.cum, self.trades, self.want = 0.0, 0, None
        self.resting = {}     # side -> {'clOrdId', 'ordId', 'px', 'qty', 'sl', 'tp'}
        self.pos = None       # {'side','entry','qty','sl','tp','ts'}
        self.done = None
        self.pnl_unconfirmed = False
        self.stopping = None  # stop requested but flatten not yet confirmed -> retried every step
        self.sleep = sleep

    def log(self, event, **kw):
        journal({'event': event, 'instId': self.inst, 'regime_id': self.rid, 'mode': 'execute' if self.execute else 'dry_run',
                 'cum_realized': round(self.cum, 4), **kw})

    def _regime_alive(self):
        try:
            st = json.loads(Path(config.DATA_DIR, 'mm_glitch_state.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        reg = (st.get('regimes') or {}).get(self.inst)
        return bool(reg and reg.get('id') == self.rid)

    @property
    def units(self):
        return float(self.spec['ctVal']) * float(self.spec.get('ctMult') or 1)

    def _place(self, side):
        px = self.L['long_entry'] if side == 1 else self.L['short_entry']
        sl = self.L['long_sl'] if side == 1 else self.L['short_sl']
        tp = self.L['long_tp'] if side == 1 else self.L['short_tp']
        tick = float(self.spec['tickSz'])
        r = lambda v: round(round(v / tick) * tick, 10)
        qty = size_qty(self.equity, px, sl, self.lev, self.cum, self.trades == 0, self.units,
                       float(self.spec['lotSz']), float(self.spec['minSz']))
        if qty <= 0:
            return self.stop('next size below minimum')
        body = {'instId': self.inst, 'tdMode': 'isolated', 'side': 'buy' if side == 1 else 'sell', 'ordType': 'limit',
                'px': str(r(px)), 'sz': str(qty), 'clOrdId': 'botr' + uuid.uuid4().hex[:24],
                'attachAlgoOrds': [{'slTriggerPx': str(r(sl)), 'slOrdPx': '-1', 'slTriggerPxType': 'last',
                                    'tpTriggerPx': str(r(tp)), 'tpOrdPx': '-1', 'tpTriggerPxType': 'last'}]}
        o = {'clOrdId': body['clOrdId'], 'px': r(px), 'qty': qty, 'sl': r(sl), 'tp': r(tp), 'ordId': None}
        if self.execute:
            if not self.trades and not self.resting:
                from okxbot.plan import mm_mark_regime_used
                mm_mark_regime_used(self.rid)
                self.c.set_leverage(self.inst, self.lev, None)
            o['ordId'] = self.c.place_order(body)[0]['ordId']
        self.resting[side] = o
        self.log('place_entry', side='long' if side == 1 else 'short', order=body)

    def _cancel_resting(self, keep=None):
        for side, o in list(self.resting.items()):
            if side == keep:
                continue
            if self.execute and o.get('ordId'):
                try:
                    self.c.cancel_order(self.inst, o['ordId'])
                except Exception as e:
                    self.log('cancel_error', error=str(e)[:200])
            del self.resting[side]
            self.log('cancel_entry', side='long' if side == 1 else 'short')

    def _filled(self, side, o, px_now):
        if self.execute:
            row = self.c.get_order(self.inst, o['ordId'])[0]
            if row.get('state') in ('filled', 'partially_filled') and float(row.get('accFillSz') or 0) > 0:
                return float(row.get('avgPx') or o['px']), float(row['accFillSz']), row.get('state') == 'filled'
            return None
        hit = px_now <= o['px'] if side == 1 else px_now >= o['px']
        return (o['px'], o['qty'], True) if hit else None

    def _exch_flat(self, px_now):
        if self.execute:
            rows = [p for p in self.c.positions() if p.get('instId') == self.inst and float(p.get('pos') or 0) != 0]
            return not rows
        p = self.pos
        return (px_now <= p['sl'] or px_now >= p['tp']) if p['side'] == 1 else (px_now >= p['sl'] or px_now <= p['tp'])

    def _set_hold(self, on):
        from okxbot.bot import load_state, save_state
        from okxbot.plan import instance_lock
        if not self.execute:
            return
        with instance_lock(30):
            st = load_state()
            if on:
                st.setdefault('positions', {})[self.inst] = {
                    'side': self.pos['side'], 'entry': self.pos['entry'], 'stop': self.pos['sl'], 'init_stop': self.pos['sl'],
                    'tp': self.pos['tp'], 'contracts': self.pos['qty'], 'lev': self.lev, 'spec': self.spec,
                    'entry_ts': int(self.pos['ts'] * 1000), 'intent': 'range_trade', 'signal': 'mm_glitch',
                    'regime_id': self.rid, 'trade_id': self.pos.get('ordId'),
                    'max_hold_until': int(min(self.pos['ts'] + config.RANGE_MAX_TRADE_S,
                                              self.start + config.RANGE_MAX_STRATEGY_S) * 1000)}
            else:
                st.get('positions', {}).pop(self.inst, None)
            save_state(st)

    def stop(self, reason):
        """Cancel entries, flatten, stop. In execute mode local state is cleared only once the exchange shows
        the position flat; on a failed/unconfirmed close the exchange stop is left in place and the stop is
        retried on the next step()."""
        if self.done:
            return
        self._cancel_resting()
        if self.pos is not None:
            if self.execute:
                try:
                    self.c.close_position(self.inst, None)
                except Exception as e:
                    self.log('flatten_error', error=str(e)[:200])
                try:
                    flat = self._exch_flat(None)
                except Exception as e:
                    self.log('flatten_check_error', error=str(e)[:200])
                    flat = False
                if not flat:
                    self.stopping = self.stopping or reason
                    self.log('flatten_unconfirmed', reason=reason, note='exchange stop kept; will retry')
                    return
                try:
                    algos = self.c.pending_algos(self.inst)
                    if algos:
                        self.c.cancel_algos([{'algoId': a['algoId'], 'instId': self.inst} for a in algos])
                except Exception as e:
                    self.log('cancel_algos_error', error=str(e)[:200])
                self._book_real('flatten: ' + reason)
            else:
                px = self.price() if self.price else None
                self._book(px if px is not None else self.pos['entry'], 'flatten: ' + reason)
            self._set_hold(False)
            self.pos = None
        self.done = reason
        self.log('strategy_stop', reason=reason, trades=self.trades)

    def _book(self, x, reason):
        """Dry-run bookkeeping (simulated exit, estimated fees)."""
        p = self.pos
        net = (x - p['entry']) * p['side'] * p['qty'] * self.units - 0.0005 * p['qty'] * self.units * (p['entry'] + x)
        self.cum += net
        self.trades += 1
        self.log('trade_closed', side='long' if p['side'] == 1 else 'short', entry=p['entry'], exit=x, qty=p['qty'],
                 net_est=round(net, 4), reason=reason)
        self.pos = None
        return net

    def _realized(self, p):
        """Exchange realized PnL for the closed position (fills + fees + funding). None if not available."""
        fn = getattr(self.c, 'positions_history', None)
        if fn is None:
            return None
        for i in range(config.RANGE_PNL_FETCH_TRIES):
            try:
                rows = fn(self.inst, 5) or []
            except Exception as e:
                self.log('pnl_fetch_error', error=str(e)[:200]); rows = []
            for h in rows:
                if int(h.get('cTime') or 0) >= int(p['ts'] * 1000) - 60_000 and h.get('realizedPnl') not in (None, ''):
                    return {'realized_pnl': float(h['realizedPnl']), 'pnl': float(h.get('pnl') or 0),
                            'fee': float(h.get('fee') or 0), 'funding': float(h.get('fundingFee') or 0),
                            'exit': float(h['closeAvgPx']) if h.get('closeAvgPx') else None}
            if i + 1 < config.RANGE_PNL_FETCH_TRIES:
                self.sleep(1)
        return None

    def _book_real(self, reason):
        """Execute-mode bookkeeping from the exchange only. Returns the realized dict or None (unconfirmed)."""
        p = self.pos
        r = self._realized(p)
        self.trades += 1
        if reason is None:
            reason = 'exchange exit' if r is None else ('tp' if r['realized_pnl'] > 0 else 'stop')
        side = 'long' if p['side'] == 1 else 'short'
        if r is None:
            self.pnl_unconfirmed = True
            self.log('trade_closed', side=side, entry=p['entry'], qty=p['qty'], realized_pnl=None, reason=reason,
                     note='realized pnl unavailable; not added to sizing profit')
        else:
            self.cum += r['realized_pnl']
            planned = p['tp'] if r['realized_pnl'] > 0 else p['sl']
            slip = abs(r['exit'] - planned) / planned if r.get('exit') and reason in ('tp', 'stop') else None
            self.log('trade_closed', side=side, entry=p['entry'], exit=r.get('exit'), qty=p['qty'], reason=reason,
                     realized_pnl=r['realized_pnl'], pnl=r['pnl'], fee=r['fee'], funding=r['funding'], slip_frac=slip)
        self.pos = None
        return r

    def step(self):
        """One iteration; returns False once the strategy has stopped. Time caps, regime checks and (execute
        mode) exchange position checks run even when the price feed is down."""
        if self.done:
            return False
        if self.stopping:
            self.stop(self.stopping)
            return not self.done
        now, px = self.clock(), self.price()
        if now - self.start >= config.RANGE_MAX_STRATEGY_S:
            self.stop('strategy 30 min cap'); return not self.done
        if not self.regime_alive():
            self.stop('regime ended or capped'); return not self.done
        if self.pos is not None:
            p = self.pos
            flat = self._exch_flat(px) if (self.execute or px is not None) else False
            if flat:
                self._set_hold(False)
                if self.execute:
                    r = self._book_real(None)
                    if r is None:
                        self.stop('realized pnl unconfirmed'); return False
                    stopped = r['realized_pnl'] <= 0
                else:
                    stopped = (px <= (p['sl'] + p['tp']) / 2) if p['side'] == 1 else (px >= (p['sl'] + p['tp']) / 2)
                    self._book(p['sl'] if stopped else p['tp'], 'stop' if stopped else 'tp')
                if stopped:
                    self.stop('stop-out'); return False
                self.want = -p['side']
            elif now - p['ts'] >= config.RANGE_MAX_TRADE_S:
                self.stop('trade 10 min cap'); return not self.done
            else:
                return True
        if px is None:
            return True
        if self.pos is None and (px <= self.L['long_sl'] or px >= self.L['short_sl']):
            self.stop('band break'); return not self.done
        if self.pos is None:
            for side, o in list(self.resting.items()):
                f = self._filled(side, o, px)
                if f:
                    self._cancel_resting(keep=side)
                    if not f[2] and self.execute and o.get('ordId'):   # partial fill: kill the remainder
                        try:
                            self.c.cancel_order(self.inst, o['ordId'])
                        except Exception as e:
                            self.log('cancel_error', error=str(e)[:200])
                    del self.resting[side]
                    self.pos = {'side': side, 'entry': f[0], 'qty': f[1], 'sl': o['sl'], 'tp': o['tp'], 'ts': now,
                                'ordId': o.get('ordId')}
                    self._set_hold(True)
                    self.log('entry_filled', side='long' if side == 1 else 'short', price=f[0], qty=f[1], full=f[2])
                    return True
            sides = [1, -1] if self.want is None else [self.want]
            for s in sides:
                if s not in self.resting and not self.done:
                    self._place(s)
        return not self.done
