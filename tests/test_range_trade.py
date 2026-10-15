"""mm_glitch band trader: validation, sizing, conservative replay, runner state machine, monitor max-hold."""
import datetime as dt, json, os, sys, tempfile, time
from pathlib import Path
os.environ.setdefault('OKXBOT_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from okxbot import config, range_trade as R, plan as P
from okxbot.monitor import signals

FIX = Path(__file__).parent / 'fixtures' / 'eth_mm_glitch_20260207_1m.json'
SPEC = {'ctVal': '0.1', 'tickSz': '0.01', 'lotSz': '0.01', 'minSz': '0.01'}


def regime(rid='R1', age=60, book_ok=True, inst='ETH-USDT-SWAP'):
    Path(config.DATA_DIR, 'mm_glitch_used.json').unlink(missing_ok=True)
    st = {'regimes': {inst: {'id': rid, 'start_s': time.time() - age, 'book_ok': book_ok}}}
    Path(config.DATA_DIR, 'mm_glitch_state.json').write_text(json.dumps(st))
    return st


def bad(fn, needle):
    try:
        fn()
    except ValueError as e:
        assert needle in str(e), (needle, str(e)); return
    raise AssertionError('expected ' + needle)


def t_validate():
    regime()
    L = R.validate('ETH-USDT-SWAP', 2053.62, 2086.28, 'R1', 20)
    assert L['long_entry'] < L['short_entry'] and L['long_sl'] < 2053.62 and L['short_sl'] > 2086.28
    bad(lambda: R.validate('DOGE-USDT-SWAP', 1, 2, 'R1', 20), 'only BTC/ETH/SOL')
    bad(lambda: R.validate('ETH-USDT-SWAP', 2053.62, 2086.28, 'R1', 21), 'leverage')
    bad(lambda: R.validate('ETH-USDT-SWAP', 2060, 2062, 'R1', 20), 'narrower')
    bad(lambda: R.validate('ETH-USDT-SWAP', 2086, 2053, 'R1', 20), 'lower < upper')
    bad(lambda: R.validate('ETH-USDT-SWAP', 2000, 2200, 'R1', 20), '60% of liquidation')
    R.validate('ETH-USDT-SWAP', 2000, 2200, 'R1', 5)       # same band fine at 5x
    bad(lambda: R.validate('ETH-USDT-SWAP', 2053.62, 2086.28, 'R2', 20), 'regime not active')
    regime(age=4000); bad(lambda: R.validate('ETH-USDT-SWAP', 2053.62, 2086.28, 'R1', 20), 'regime not active')
    regime(book_ok=False); bad(lambda: R.validate('ETH-USDT-SWAP', 2053.62, 2086.28, 'R1', 20), 'book depth')
    regime(); P.mm_mark_regime_used('R1'); bad(lambda: R.validate('ETH-USDT-SWAP', 2053.62, 2086.28, 'R1', 20), 'already used')


def t_sizing():
    eq = 90936.0
    q = R.size_qty(eq, 2000, 1990, 20, 0, True, 0.1, 0.01, 0.01)
    assert abs(q * 0.1 * (10 + 2000 * config.RANGE_COST_FRAC) - eq * config.RISK_PER_TRADE) < 1   # first: 0.5% risk incl. costs
    q2 = R.size_qty(eq, 2000, 1990, 20, 300, False, 0.1, 0.01, 0.01)     # margin 100 -> notional 2000 -> 1 ETH
    assert abs(q2 - 10) < 0.011, q2
    q3 = R.size_qty(eq, 2000, 1990, 20, 10 ** 7, False, 0.1, 0.01, 0.01)
    assert q3 * 0.1 * 14 <= eq * config.RISK_PER_TRADE + 1e-6            # still risk-capped
    assert R.size_qty(eq, 2000, 1990, 20, 0, False, 0.1, 0.01, 0.01) == 0
    assert R.size_qty(eq, 2000, 1990, 20, -5, False, 0.1, 0.01, 0.01) == 0
    assert R.size_qty(eq, 2000, 1990, 20, 0.003, False, 0.1, 0.01, 0.01) == 0   # below min size


def bar(ts, o, h, l, c):
    return {'ts': ts * 60000, 'o': o, 'h': h, 'l': l, 'c': c}


def t_replay_rules():
    # band 100-110: long entry 101.75, tp 109, sl 99; short entry 108.25, tp 101, sl 111
    bs = [bar(0, 105, 105, 101.5, 102), bar(1, 102, 109.5, 102, 109),      # long fills, TP next bar
          bar(2, 109, 109, 108, 108.5),                                      # short fills (alternate)
          bar(3, 108, 108.5, 100.5, 101)]                                    # short TP
    r = R.replay(bs, 100, 110, lev=5, units=1, lot=0.001, min_sz=0.001)
    assert [t['side'] for t in r['trades']] == ['long', 'short'] and all(t['reason'] == 'tp' for t in r['trades'])
    assert r['total_net'] > 0 and r['trades'][1]['qty'] < r['trades'][0]['qty']   # 2nd sized from 1/3 profit
    # TP never credited in the entry bar
    r = R.replay([bar(0, 105, 109.5, 101.5, 105)], 100, 110, lev=5)
    assert not r['trades'] or r['trades'][0]['reason'].startswith('flatten')
    # entry + stop in same bar -> stop, strategy ends
    r = R.replay([bar(0, 105, 105, 98.5, 100), bar(1, 100, 105, 100, 104)], 100, 110, lev=5)
    assert r['trades'][0]['reason'] == 'stop' and len(r['trades']) == 1 and r['stop_reason'] == 'stop'
    assert r['max_drawdown'] < 0
    # TP and SL both inside a later bar -> SL
    r = R.replay([bar(0, 105, 105, 101.5, 102), bar(1, 102, 109.5, 98, 105)], 100, 110, lev=5)
    assert r['trades'][0]['reason'] == 'stop'
    # band break while flat
    r = R.replay([bar(0, 105, 112, 104, 111)], 100, 110, lev=5)   # short entry then stop, same bar
    assert r['trades'][0]['side'] == 'short' and r['trades'][0]['reason'] == 'stop'
    r = R.replay([bar(0, 105, 106, 104, 105), bar(1, 112, 113, 111.5, 112)], 100, 110, lev=5)
    assert not r['trades'] and r['stop_reason'] == 'band break while flat'


def t_replay_feb7():
    d = json.loads(FIX.read_text())
    bs = [{'ts': int(x[0]), 'o': float(x[1]), 'h': float(x[2]), 'l': float(x[3]), 'c': float(x[4])} for x in d['candles']]
    i = next(k for k, b in enumerate(bs) if dt.datetime.fromtimestamp(b['ts'] / 1e3, dt.timezone.utc).strftime('%H:%M') == '16:09')
    r = R.replay(bs, 2053.62, 2086.28, start_idx=i)
    t = r['trades']     # wicks blow through the close band: 16:09 long fills at open, stopped in-bar
    assert len(t) == 1 and t[0]['side'] == 'long' and t[0]['reason'] == 'stop'
    assert -t[0]['net'] <= 90936 * config.RISK_PER_TRADE * 1.02      # loss incl. fees/slippage stays ~0.5%
    r = R.replay(bs, 2016.0, 2092.12, start_idx=i)
    assert r['trades'][0]['side'] == 'short' and r['trades'][0]['reason'] == 'tp'


class FakeClient:
    has_keys = True

    def __init__(self):
        self.orders, self.calls, self.pos = {}, [], []

    def set_leverage(self, *a): self.calls.append(('lev',) + a); return [{}]
    def place_order(self, b):
        oid = str(len(self.orders) + 1); self.orders[oid] = dict(b, state='live'); self.calls.append(('place', b))
        return [{'ordId': oid}]
    def cancel_order(self, inst, oid): self.orders[oid]['state'] = 'canceled'; self.calls.append(('cancel', oid))
    def get_order(self, inst, oid):
        o = self.orders[oid]
        return [{'state': o['state'], 'accFillSz': o['sz'] if o['state'] == 'filled' else '0', 'avgPx': o['px']}]
    def positions(self): return self.pos
    def close_position(self, inst, ps): self.calls.append(('close', inst)); self.pos = []
    def pending_algos(self, inst): return [{'algoId': 'a1'}]
    def cancel_algos(self, items): self.calls.append(('cancel_algos', items))


def runner(px, execute=False, client=None, alive=lambda: True, clock=None):
    regime()
    return R.Runner(client or FakeClient(), 'ETH-USDT-SWAP', 100, 110, 'R1', 5, execute=execute,
                    price=lambda: px[0], regime_alive=alive, spec=SPEC, equity=10000, clock=clock or time.time)


def t_runner_dry_alternates_and_stops():
    px = [105.0]
    r = runner(px)
    r.step(); assert set(r.resting) == {1, -1} and not r.execute
    px[0] = 101.7; r.step(); assert r.pos['side'] == 1 and not r.resting      # long filled, short cancelled
    px[0] = 109.2; r.step(); assert r.pos is None and r.want == -1 and r.cum > 0
    assert list(r.resting) == [-1]                                             # only the opposite side
    px[0] = 108.3; r.step(); assert r.pos['side'] == -1
    px[0] = 111.5; r.step(); assert r.done == 'stop-out' and r.pos is None and not r.resting
    assert r.step() is False


def t_runner_exit_conditions():
    px = [105.0]
    r = runner(px, alive=lambda: False); r.step(); assert r.done == 'regime ended or capped'
    t = [1000.0]
    r = runner(px, clock=lambda: t[0]); r.step(); t[0] += config.RANGE_MAX_STRATEGY_S; r.step()
    assert r.done == 'strategy 30 min cap'
    px = [112.0]; r = runner(px); r.step(); assert r.done == 'band break'
    px = [105.0]; t = [0.0]; r = runner(px, clock=lambda: t[0]); r.step(); px[0] = 101.7; r.step()
    t[0] += config.RANGE_MAX_TRADE_S; px[0] = 103; r.step(); assert r.done == 'trade 10 min cap'
    # zero profit -> no second entry: first trade closed flat at TP==entry is impossible; force cum<=0
    px = [105.0]; r = runner(px); r.trades, r.cum = 1, 0.0; r.step(); assert r.done == 'next size below minimum'


def t_runner_execute_orders_and_flatten():
    old = config.RANGE_TRADE_ENABLED
    try:
        config.RANGE_TRADE_ENABLED = False
        assert runner([105.0], execute=True).execute is False            # gate off -> dry-run
        config.RANGE_TRADE_ENABLED = True
        c = FakeClient(); px = [105.0]
        r = runner(px, execute=True, client=c); assert r.execute
        r.step()
        places = [x[1] for x in c.calls if x[0] == 'place']
        assert len(places) == 2 and all(b['tdMode'] == 'isolated' and b['ordType'] == 'limit' for b in places)
        assert all(b['attachAlgoOrds'][0]['slTriggerPx'] and b['attachAlgoOrds'][0]['tpTriggerPx'] for b in places)
        assert ('lev', 'ETH-USDT-SWAP', 5, None) in c.calls and 'R1' in P.mm_used_regimes()
        buy = next(k for k, o in c.orders.items() if o['side'] == 'buy'); c.orders[buy]['state'] = 'filled'
        c.pos = [{'instId': 'ETH-USDT-SWAP', 'pos': '1'}]
        r.step(); assert r.pos and any(x[0] == 'cancel' for x in c.calls)
        from okxbot.bot import load_state
        rec = load_state()['positions']['ETH-USDT-SWAP']
        assert rec['signal'] == 'mm_glitch' and rec['max_hold_until'] <= (time.time() + config.RANGE_MAX_TRADE_S) * 1000 + 5
        r.stop('test'); assert ('close', 'ETH-USDT-SWAP') in c.calls and any(x[0] == 'cancel_algos' for x in c.calls)
        assert 'ETH-USDT-SWAP' not in load_state().get('positions', {})
        rows = [json.loads(l) for l in Path(config.DATA_DIR, 'decision_journal.jsonl').read_text().splitlines()]
        assert {'place_entry', 'entry_filled', 'strategy_stop'} <= {x.get('event') for x in rows if x.get('kind') == 'range_trade'}
    finally:
        config.RANGE_TRADE_ENABLED = old


def t_monitor_enforces_max_hold():
    from okxbot.monitor.service import Monitor
    m = Monitor.__new__(Monitor)
    c = FakeClient(); m.client = c
    assert m.enforce_max_hold('ETH-USDT-SWAP', {'signal': 'mm_glitch'}) == 'closed'
    assert ('close', 'ETH-USDT-SWAP') in c.calls
    assert m.enforce_max_hold('BTC-USDT-SWAP', {}) == 'not mm_glitch'
    c.has_keys = False
    assert m.enforce_max_hold('ETH-USDT-SWAP', {'signal': 'mm_glitch'}) == 'no keys (log-only)'
    ev = signals.position_checks('ETH-USDT-SWAP', {'max_hold_until': 1, 'signal': 'mm_glitch'}, None, None, {},
                                 signals.Armed(), {}, 10.0)
    assert ev and ev[0]['kind'] == 'max_hold_exceeded'     # fires even without a price feed


if __name__ == '__main__':
    for name, func in list(globals().items()):
        if name.startswith('t_'):
            func(); print('PASS', name)
