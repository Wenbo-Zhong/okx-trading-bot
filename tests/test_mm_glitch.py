"""mm_glitch detector, regime wakes, and planner guardrails. Offline; replays OKX 1m data of the
2026-02-07 ETH market-maker malfunction (tests/fixtures/eth_mm_glitch_20260207_1m.json)."""
import datetime as dt, json, os, sys, tempfile, time
from pathlib import Path
os.environ.setdefault('OKXBOT_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from okxbot import config, plan as P
from okxbot.bot import save_state
from okxbot.monitor.mmglitch import MMGlitch
from okxbot.monitor.wake import Waker
from okxbot.monitor import signals

FIX = Path(__file__).parent / 'fixtures' / 'eth_mm_glitch_20260207_1m.json'
B = lambda r: {"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
hm = lambda ts: dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).strftime('%H:%M')


def replay(cfg=None, depth=5e6, seed=60, state_path=None):
    d = json.loads(FIX.read_text())
    m = MMGlitch(cfg, depth_of=lambda i: depth, state_path=state_path)
    for r in d['index_candles']:
        m.add_index('ETH-USDT', B(r))
    cs = [B(r) for r in d['candles']]
    m.seed('ETH-USDT-SWAP', cs[:seed])
    out = []
    for b in cs[seed:]:
        out += m.on_bar('ETH-USDT-SWAP', b)
    return m, out


def t_replay_feb7_event():
    _, ev = replay()
    kinds = [(hm(e['bar_ts']), e['kind']) for e in ev if e['priority'] != 'low']
    assert kinds[0] == ('16:05', 'mm_glitch_trigger'), kinds
    assert ('16:08', 'mm_glitch_confirmed') in kinds, kinds
    assert kinds[-1][1] == 'mm_glitch_end' and '16:20' <= kinds[-1][0] <= '16:35', kinds
    # nothing fires during the calm 15:00-16:04 baseline
    assert all(hm(e['bar_ts']) >= '16:05' for e in ev)
    wakes = [e for e in ev if e.get('regime_wake')]
    assert 3 <= len(wakes) <= 15
    assert all(e['observation_only'] and e['no_auto_execute'] and e['signal'] == 'mm_glitch' for e in ev)
    conf = next(e for e in ev if e['kind'] == 'mm_glitch_confirmed')
    assert conf['detail']['book_ok'] is True and conf['detail']['max_index_dev_bp'] >= 30
    print('  replay:', [k for k in kinds])


def t_thin_book_flagged():
    _, ev = replay(depth=1e5)
    conf = next(e for e in ev if e['kind'] == 'mm_glitch_confirmed')
    assert conf['detail']['book_ok'] is False


def t_caps_flatten_and_daily_limit():
    _, ev = replay({'mm_max_wakes_per_regime': 3, 'mm_update_band_frac': 0})   # every update wake-worthy
    cap = [e for e in ev if e['kind'] == 'mm_glitch_cap_flatten']
    assert cap and 'FLATTEN' in cap[0]['detail']['instruction'] and cap[0]['severe']
    assert sum(1 for e in ev if e.get('regime_wake') and e['kind'] in ('mm_glitch_confirmed', 'mm_glitch_update')) <= 3
    _, ev = replay({'mm_max_regime_s': 240})
    cap = [e for e in ev if e['kind'] == 'mm_glitch_cap_flatten']
    assert cap and cap[0]['detail']['age_s'] >= 240
    m, _ = replay({'mm_max_regimes_per_day': 0})
    assert not m.regime   # regime cap reached -> confirmation logged at low priority only
    _, ev = replay({'mm_max_regimes_per_day': 0})
    c = next(e for e in ev if e['kind'] == 'mm_glitch_confirmed')
    assert c['priority'] == 'low' and not c.get('regime_wake')


def t_regime_wakes_share_budget():
    """Regression (Codex #5): mm_glitch wakes no longer bypass the hourly/daily caps; they share the budget."""
    clock = [1_000_000.0]
    cfg = {'coalesce_s': 90, 'wake_min_interval_s': 3600, 'wake_max_per_day': 8, 'fault_cooldown_s': 900,
           'wake_max_per_week': 30, 'wake_fault_reserve_week': 6}
    d = tempfile.mkdtemp()
    w = Waker(cfg, clock=lambda: clock[0], log_path=d + '/l', state_path=d + '/s')
    w.add({'kind': 'breakout_up', 'inst': 'X', 'bar': '1H', 'bar_ts': 1})
    clock[0] += 100
    assert w.tick()                       # normal wake uses the hourly slot
    sent = 0
    for i in range(15):                   # a whole regime's worth of updates within the hour
        w.add({'kind': 'mm_glitch_update', 'inst': 'ETH', 'bar': '1m', 'bar_ts': 10 + i, 'regime_wake': True,
               'signal': 'mm_glitch', 'detail': {'band': [100, 101 + i]}})
        sent += bool(w.tick())
        clock[0] += 90
    assert sent == 0 and w.st['count'] == 1   # held by the shared hourly cap
    clock[0] += 3600
    assert w.tick() and w.st['count'] == 2    # then coalesced into ONE wake
    # daily limit 0 -> nothing dispatches, regime or not
    w0 = Waker(dict(cfg, wake_max_per_day=0), clock=lambda: clock[0], log_path=d + '/l0', state_path=d + '/s0')
    for i in range(15):
        w0.add({'kind': 'mm_glitch_update', 'inst': 'ETH', 'bar': '1m', 'bar_ts': 100 + i, 'regime_wake': True,
                'signal': 'mm_glitch'})
        assert w0.tick() is None
        clock[0] += 90
    w0.add({'kind': 'mm_glitch_cap_flatten', 'inst': 'ETH', 'bar': '1m', 'bar_ts': 999, 'severe': True,
            'regime_wake': True, 'signal': 'mm_glitch'})
    assert w0.tick() is None and w0.st['count'] == 0


def t_update_wakes_only_on_band_change():
    m, ev = replay()
    ups = [e for e in ev if e['kind'] == 'mm_glitch_update']
    assert ups and all(e['detail']['band_move_frac'] >= 0.25 for e in ups)
    ticks = [e for e in ev if e['kind'] == 'mm_glitch_tick']
    assert ticks and all(e['priority'] == 'low' for e in ticks)


def mm_fixture(lev=20, stop=98.5, **kw):
    for name in ('decision_journal.jsonl', 'state.json', 'mm_glitch_used.json'):
        Path(config.DATA_DIR, name).unlink(missing_ok=True)
    state = {'positions': {}, 'spot': {'longs': {}}, 'last_signal_ts': {}}
    save_state(state)
    spec = {'instId': 'ETH-USDT-SWAP', 'state': 'live', 'tickSz': '.01', 'lotSz': '.01', 'minSz': '.01',
            'ctVal': '0.1', 'maxLever': '100'}
    snap = {'snapshot_id': 's', 'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'account': {'equity_usdt': 10000, 'avail_usdt': 10000, 'positions': [], 'pending_orders': [], 'pending_algos': []},
            'guardrails': {'can_open': True},
            'instruments': {i: {'spec': dict(spec, instId=i), 'last': 100., 'mark': 100., 'liquidity_ok': True}
                            for i in ('ETH-USDT-SWAP', 'DOGE-USDT-SWAP')}}
    Path(config.DATA_DIR, 'mm_glitch_state.json').write_text(json.dumps(
        {'regimes': {'ETH-USDT-SWAP': {'id': 'R1', 'start_s': time.time() - 60, 'book_ok': True}}}))
    a = {'action': 'open', 'instId': 'ETH-USDT-SWAP', 'market': 'swap', 'side': 'long', 'leverage': lev,
         'stop': stop, 'size': {'risk_usdt': 50}, 'signal': 'mm_glitch', 'regime_id': 'R1', **kw}
    return a, snap, state


def rejects(a, snap, st, needle):
    try:
        P.validate_action(a, snap, st)
    except ValueError as e:
        assert needle in str(e), (needle, str(e))
        return
    raise AssertionError('expected rejection: ' + needle)


def t_plan_guardrails():
    a, s, st = mm_fixture()
    order, _ = P.validate_action(a, s, st)
    assert order['tdMode'] == 'isolated' and order['attachAlgoOrds'][0]['slTriggerPx'] == '98.5'
    risk = float(order['sz']) * 0.1 * 1.5
    assert risk <= 10000 * config.RISK_PER_TRADE + 1e-6
    # 20x without the tag stays capped by global MAX_LEVERAGE (5)
    a, s, st = mm_fixture(); a.pop('signal'); rejects(a, s, st, 'leverage exceeds allowed cap')
    a, s, st = mm_fixture(lev=21); rejects(a, s, st, 'leverage exceeds allowed cap')
    # 20x: liq distance 0.044, 60% -> 2.64%; 3% stop rejected
    a, s, st = mm_fixture(stop=97.0); rejects(a, s, st, 'liq distance')
    a, s, st = mm_fixture(regime_id='OTHER'); rejects(a, s, st, 'regime not active')
    a, s, st = mm_fixture(); a.pop('regime_id'); rejects(a, s, st, 'regime_id')
    a, s, st = mm_fixture(instId='DOGE-USDT-SWAP'); rejects(a, s, st, 'only BTC/ETH/SOL')
    a, s, st = mm_fixture(signal='yolo'); rejects(a, s, st, 'unknown signal')
    a, s, st = mm_fixture()
    Path(config.DATA_DIR, 'mm_glitch_state.json').write_text(json.dumps(
        {'regimes': {'ETH-USDT-SWAP': {'id': 'R1', 'start_s': time.time() - 4000, 'book_ok': True}}}))
    rejects(a, s, st, 'regime not active')
    a, s, st = mm_fixture()
    Path(config.DATA_DIR, 'mm_glitch_state.json').write_text(json.dumps(
        {'regimes': {'ETH-USDT-SWAP': {'id': 'R1', 'start_s': time.time(), 'book_ok': False}}}))
    rejects(a, s, st, 'book depth')
    a, s, st = mm_fixture(); P.mm_mark_regime_used('R1'); rejects(a, s, st, 'one entry per regime')
    # risk above RISK_PER_TRADE via contracts is rejected
    a, s, st = mm_fixture(size={'contracts': 5000}); rejects(a, s, st, 'risk exceeds per-trade cap')
    # no averaging down: an existing ETH position blocks a second entry
    a, s, st = mm_fixture(); s['account']['positions'] = [{'instId': 'ETH-USDT-SWAP'}]
    rejects(a, s, st, 'duplicate coin')
    assert config.MAX_LEVERAGE == 5 and config.MM_GLITCH_MAX_LEVERAGE == 20 and config.TD_MODE == 'isolated'


def t_max_hold_close_and_monitor_fault():
    from okxbot.bot import Bot
    b = Bot.__new__(Bot)
    closed = []
    b.st = {'positions': {'ETH-USDT-SWAP': {'side': 1, 'max_hold_until': 1000, 'signal': 'mm_glitch'},
                          'BTC-USDT-SWAP': {'side': 1}}}
    b.close = lambda inst, ps, reason: closed.append((inst, reason))
    b.pos_mode = 'net_mode'
    b.enforce_max_hold(now_ms=2000)
    b.enforce_max_hold(now_ms=3000)
    assert closed == [('ETH-USDT-SWAP', 'max hold exceeded (mm_glitch)')]
    pos = {'side': 1, 'entry': 100, 'stop': 98.5, 'max_hold_until': 1000, 'signal': 'mm_glitch'}
    ev = signals.position_checks('ETH-USDT-SWAP', pos, 100.0, None, {'stop_breach_grace_s': 120, 'stop_proximity_atr': .3,
                                 'stop_proximity_rearm_atr': .8, 'adverse_r': .7, 'adverse_rearm_r': .4},
                                 signals.Armed(), {}, 5.0)
    assert any(e['kind'] == 'max_hold_exceeded' and e['severe'] for e in ev)


if __name__ == '__main__':
    for name, func in list(globals().items()):
        if name.startswith('t_'):
            func(); print('PASS', name)
