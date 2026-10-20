"""Planner regression tests: entirely offline, with a recording fake exchange."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
os.environ.setdefault('OKXBOT_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from okxbot import config, plan as P
from okxbot.bot import save_state

NOW = dt.datetime.now(dt.timezone.utc)
SPEC = {'instId': 'BTC-USDT', 'state': 'live', 'tickSz': '.01', 'lotSz': '.001', 'minSz': '.001', 'ctVal': '1'}


def fixture(action=None):
    for name in ('decision_journal.jsonl', 'state.json'):
        Path(config.DATA_DIR, name).unlink(missing_ok=True)
    state = {'positions': {}, 'spot': {'longs': {}}, 'last_signal_ts': {}}
    save_state(state)
    snapshot = {'snapshot_id': 'test-snapshot', 'generated_at': NOW.isoformat(),
                'account': {'equity_usdt': 10000, 'avail_usdt': 10000, 'positions': [], 'pending_orders': [], 'pending_algos': []},
                'guardrails': {'can_open': True},
                'instruments': {i: {'spec': dict(SPEC, instId=i), 'last': 100., 'mark': 100., 'liquidity_ok': True}
                                for i in ('BTC-USDT', 'BTC-USDT-SWAP')}}
    path = Path(config.DATA_DIR, 'snapshots', 'test-snapshot.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot))
    plan = {'schema': 'okxbot.plan/v1', 'plan_id': 'test-plan', 'snapshot_id': snapshot['snapshot_id'],
            'created_at': (NOW-dt.timedelta(minutes=1)).isoformat(), 'valid_until': (NOW+dt.timedelta(hours=1)).isoformat(),
            'rationale': 'Explicit test decision', 'uncertainties': [], 'actions': [action or opening()]}
    return plan, snapshot, state, path


def opening(**kw):
    return {'action': 'open', 'instId': 'BTC-USDT', 'market': 'spot', 'side': 'long',
            'stop': 97., 'take_profit': 106., 'size': {'notional_usdt': 100}, **kw}


class Fake:
    has_keys = True
    def __init__(self):
        self.posts, self.orders, self.algos = [], {}, []
        self.qty = 0
        self.fill_fraction = 1
    def account_config(self): return [{'posMode': 'net_mode'}]
    def balance(self):
        return [{'totalEq': '10000', 'details': [{'ccy': 'USDT', 'availEq': '10000', 'availBal': '10000'},
                                                {'ccy': 'BTC', 'cashBal': str(self.qty), 'eq': str(self.qty), 'availBal': str(self.qty)}]}]
    def positions(self): return []
    def pending_algos(self, inst_id=None, inst_type=None):
        return [copy.deepcopy(a) for a in self.algos if inst_id is None or a['instId'] == inst_id]
    def max_size(self, *a): return [{'maxBuy': '10000', 'maxSell': '10000'}]
    def _req(self, method, path, params=None, **kwargs):
        assert method == 'GET', 'unexpected network write'
        if path.endswith('orders-pending'): return []
        if path.endswith('instruments'): return [dict(SPEC, instId=params['instId'])]
        if path.endswith('ticker'): return [{'last': '100'}]
        if path.endswith('mark-price'): return [{'markPx': '100'}]
        if path.endswith('books'): return [{'asks': [['100.01', '1000000']], 'bids': [['99.99', '1000000']]}]
        if path.endswith('/order'):
            return [next(v for v in self.orders.values() if v['clOrdId'] == params['clOrdId'])]
        raise AssertionError(path)
    def place_order(self, body):
        self.posts.append(('order', copy.deepcopy(body)))
        oid = str(len(self.orders)+1)
        qty = float(body['sz']) / (100 if body.get('tgtCcy') else 1)
        qty *= self.fill_fraction
        self.qty += qty if body['side'] == 'buy' else -qty
        self.orders[oid] = {'ordId': oid, 'clOrdId': body['clOrdId'], 'state': 'filled', 'accFillSz': str(qty), 'avgPx': '100'}
        return [{'ordId': oid, 'sCode': '0'}]
    def get_order(self, inst, oid): return [self.orders[oid]]
    def place_algo(self, body):
        self.posts.append(('algo', copy.deepcopy(body)))
        aid = str(len(self.posts))
        self.algos.append(dict(body, algoId=aid))
        return [{'algoId': aid, 'sCode': '0'}]
    def cancel_algos(self, rows):
        self.posts.append(('cancel', rows))
        self.algos = [a for a in self.algos if a['algoId'] not in {r['algoId'] for r in rows}]
        return [{'sCode': '0'}]
    def amend_algo(self, body):
        self.posts.append(('amend', body))
        for a in self.algos:
            if a['algoId'] == body['algoId']:
                for key in ('Sl', 'Tp'):
                    if 'new'+key+'TriggerPx' in body:
                        a[key.lower()+'TriggerPx'] = body['new'+key+'TriggerPx']
        return [{'sCode': '0'}]
    def set_leverage(self, *args):
        self.posts.append(('leverage', args)); return [{'sCode': '0'}]
    def cancel_order(self, *args):
        self.posts.append(('cancel_order', args)); return [{'sCode': '0'}]


def t_plan_schema_and_dates():
    p, s, st, path = fixture()
    for changes in ({'schema': 'wrong'}, {'rationale': ''}, {'uncertainties': None}, {'actions': [{}]*7},
                    {'created_at': '2026-01-01'}, {'created_at': (NOW+dt.timedelta(hours=1)).isoformat()},
                    {'valid_until': (NOW+dt.timedelta(hours=7)).isoformat()}):
        result = P.run_plan(dict(p, **changes), client=Fake())
        assert not result['valid'], changes
    for changes in ({'valid_until': (NOW-dt.timedelta(seconds=1)).isoformat()},):
        x = Fake(); result = P.run_plan(dict(p, **changes), execute=True, client=x)
        assert not result['valid'] and not x.posts
    s['generated_at'] = (NOW-dt.timedelta(hours=1)).isoformat(); path.write_text(json.dumps(s))
    x = Fake(); assert not P.run_plan(p, True, client=x)['valid'] and not x.posts


def t_open_validation():
    p, s, st, path = fixture()
    for changes in ({'stop': 101}, {'stop': 90}, {'stop': 99.9}, {'leverage': 6}, {'side': 'short'},
                    {'size': {'contracts': 1000}}, {'size': {'risk_usdt': 1, 'contracts': 1}},
                    {'size': {'risk_usdt': True}}, {'take_profit': 99}, {'order_type': 'limit'},
                    {'leverage': True}, {'stop': float('nan')}):
        try: P.validate_action(opening(**changes), s, st)
        except (ValueError, KeyError): pass
        else: raise AssertionError(changes)
    order, notes = P.validate_action(opening(size={'risk_usdt': 1000}), s, st)
    assert any('clamped' in n for n in notes)
    assert float(order['sz']) <= 10000 * config.SPOT_RISK_PER_TRADE / .03
    s['account']['positions'] = [{'instId': 'BTC-USDT-SWAP'}]
    try: P.validate_action(opening(), s, st)
    except ValueError as e: assert 'duplicate coin' in str(e)
    else: assert False


def t_dry_run_no_posts_and_reservation():
    p, s, st, path = fixture()
    p['actions'].append(opening(instId='BTC-USDT-SWAP', market='swap'))
    x = Fake(); result = P.run_plan(p, client=x)
    assert result['valid'] and result['actions'][0]['status'] == 'would_execute', result
    assert result['actions'][1]['status'] == 'rejected' and not x.posts
    json.dumps(result, sort_keys=True, ensure_ascii=False)


def t_execute_entry_stop_duplicate_and_hold():
    p, s, st, path = fixture()
    p['actions'].append({'action': 'hold', 'reason': 'wait'})
    x = Fake(); result = P.run_plan(p, True, client=x)
    assert result['actions'][0]['status'] == 'executed', result
    assert any(k == 'order' for k, v in x.posts) and any(k == 'algo' for k, v in x.posts)
    assert result['actions'][1]['status'] == 'skipped'
    assert not P.run_plan(p, True, client=x)['valid']
    rows = [json.loads(l) for l in Path(config.DATA_DIR, 'decision_journal.jsonl').read_text().splitlines()]
    assert any(r['actions'] and r['actions'][-1]['action'] == 'hold' for r in rows)


def t_partial_close_and_amend():
    p, s, st, path = fixture()
    x = Fake(); assert P.run_plan(p, True, client=x)['actions'][0]['status'] == 'executed'
    before = x.qty
    p['plan_id'] = 'close-plan'
    p['actions'] = [{'action': 'amend_sl', 'instId': 'BTC-USDT', 'stop': 101},
                    {'action': 'close', 'instId': 'BTC-USDT', 'fraction': .5, 'reason': 'reduce'}]
    x.fill_fraction = .5
    result = P.run_plan(p, True, client=x)
    assert result['actions'][0]['status'] == 'rejected', result
    assert result['actions'][1]['status'] == 'executed', result
    rec = P.load_state()['spot']['longs']['BTC-USDT']
    assert 0 < rec['qty'] < before and rec['qty'] > before / 2
    assert sum(float(a['sz']) for a in x.algos) >= rec['qty']
    p['plan_id'] = 'amend-plan'
    p['actions'] = [{'action': 'amend_sl', 'instId': 'BTC-USDT', 'stop': 96}]
    assert P.run_plan(p, True, client=x)['actions'][0]['status'] == 'executed'
    assert P.load_state()['spot']['longs']['BTC-USDT']['stop'] == 96


def t_keys_demo_and_lock():
    p, s, st, path = fixture()
    x = Fake(); x.has_keys = False
    assert not P.run_plan(p, True, client=x)['valid'] and not x.posts
    old = config.DEMO
    try:
        config.DEMO = False
        assert not P.run_plan(p, True, client=Fake())['valid']
    finally: config.DEMO = old
    with P.instance_lock():
        result = P.run_plan(p, lock_timeout=0)
        assert not result['valid'] and 'lock' in result['plan_errors'][0]


def t_snapshot_matching_cancel_and_guardrail():
    p, s, st, path = fixture()
    p['snapshot_id'] = 'different'
    assert not P.run_plan(p, snapshot_path=path)['valid']
    st['halted'] = True
    try: P.validate_action(opening(), s, st)
    except ValueError as e: assert 'guardrails' in str(e)
    else: assert False
    a = {'action': 'cancel', 'instId': 'BTC-USDT', 'ordId': '1'}
    try: P.validate_action(a, s, st)
    except ValueError: pass
    else: assert False
    s['account']['pending_orders'] = [{'instId': 'BTC-USDT', 'ordId': '1'}]
    assert P.validate_action(a, s, st)[0]['ordId'] == '1'


def t_swap_execution_and_limit():
    p, s, st, path = fixture(opening(instId='BTC-USDT-SWAP', market='swap', side='short',
                                      stop=103., take_profit=94., order_type='limit', limit_px=100.))
    x = Fake()
    result = P.run_plan(p, True, client=x)
    assert result['actions'][0]['status'] == 'executed', result
    order = next(v for k, v in x.posts if k == 'order')
    assert order['ordType'] == 'limit' and order['px'] == '100.0' and order['side'] == 'sell'
    assert P.load_state()['positions']['BTC-USDT-SWAP']['stop'] == 103
    assert any(a.get('slTriggerPx') for a in x.algos)


def t_unknown_submission_recovery_no_duplicate():
    for retain_id in (True, False):
        p, s, st, path = fixture()
        x = Fake()
        original = x.place_order
        def interrupted(body):
            response = original(body)
            if retain_id:
                # Simulate failure on the first order status read, after ordId save.
                x.get_order = lambda *a: (_ for _ in ()).throw(RuntimeError('status outage'))
                return response
            raise RuntimeError('response lost after exchange accepted')
        x.place_order = interrupted
        result = P.run_plan(p, True, client=x)
        assert result['actions'][0]['status'] == 'failed'
        state = P.load_state()
        assert state['plan_pending']
        x.get_order = lambda inst, oid: [x.orders[oid]]
        b = P.make_bot(x); b.pos_mode = 'net_mode'
        assert P.recover_plan_orders(b)
        assert len([v for k, v in x.posts if k == 'order']) == 1
        assert b.st['spot']['longs']['BTC-USDT']['stop'] == 97
        assert not b.st['plan_pending']
        assert not P.run_plan(p, True, client=x)['valid']


def t_tp_amend_remove_and_cancel():
    p, s, st, path = fixture()
    x = Fake(); assert P.run_plan(p, True, client=x)['actions'][0]['status'] == 'executed'
    for pid, target in (('tp-new', 107), ('tp-remove', None), ('tp-restore', 108)):
        p.update(plan_id=pid, actions=[{'action': 'amend_tp', 'instId': 'BTC-USDT', 'take_profit': target}])
        result = P.run_plan(p, True, client=x)
        assert result['actions'][0]['status'] == 'executed', result
        assert P.load_state()['spot']['longs']['BTC-USDT']['tp'] == target
    tp = next(a for a in x.algos if a.get('tpTriggerPx'))
    p.update(plan_id='cancel', actions=[{'action': 'cancel', 'instId': 'BTC-USDT', 'algoId': tp['algoId']}])
    assert P.run_plan(p, True, client=x)['actions'][0]['status'] == 'executed'
    assert any(a.get('slTriggerPx') for a in x.algos)


def t_failed_protection_exits_spot():
    p, s, st, path = fixture()
    x = Fake()
    from okxbot.okx_client import OKXError
    def fail(body): raise OKXError('stop rejected')
    x.place_algo = fail
    result = P.run_plan(p, True, client=x)
    assert result['actions'][0]['status'] == 'failed', result
    assert any(k == 'order' and v['side'] == 'sell' for k, v in x.posts)
    assert x.qty == 0


def t_cli_one_json_and_no_keys():
    import subprocess
    p, s, st, path = fixture({'action': 'hold', 'reason': 'wait'})
    plan_file = Path(config.DATA_DIR, 'plan.json'); plan_file.write_text(json.dumps(p))
    completed = subprocess.run([sys.executable, '-m', 'scripts.execute_plan', str(plan_file)],
                               capture_output=True, text=True, check=True)
    result = json.loads(completed.stdout)
    assert result['mode'] == 'dry_run' and result['actions'][0]['status'] == 'would_execute'


def t_remaining_guardrails_and_size_clamps():
    p, s, st, path = fixture()
    cases = [({'account': dict(s['account'], positions=[{'instId': i} for i in ('ETH-USDT', 'SOL-USDT', 'XRP-USDT')])}, 'MAX_POSITIONS'),
             ({'guardrails': {'can_open': False}}, 'guardrails')]
    for changes, reason in cases:
        try: P.validate_action(opening(), dict(s, **changes), st)
        except ValueError as e: assert reason in str(e)
        else: assert False
    for a in (opening(size={'notional_usdt': 3000}, stop=99),
              opening(size={'contracts': .00001}),
              {'action': 'unknown'}, {'action': 'close', 'instId': 'BTC-USDT', 'reason': 'none'}):
        try: P.validate_action(a, s, st)
        except ValueError: pass
        else: assert False
    d = s['instruments']['BTC-USDT']
    d.update(exchange_max=.5, depth_cap=1000)
    d['spec']['maxMktAmt'] = '20'
    order, notes = P.validate_action(opening(), s, st)
    assert float(order['sz']) <= 20 and notes
    d['depth_cap'] = 0
    try: P.validate_action(opening(), s, st)
    except ValueError: pass
    else: assert False


def t_partial_swap_close_keeps_stop():
    p, s, st, path = fixture(opening(instId='BTC-USDT-SWAP', market='swap'))
    x = Fake(); assert P.run_plan(p, True, client=x)['actions'][0]['status'] == 'executed'
    b = P.make_bot(x); b.pos_mode = 'net_mode'
    inst = 'BTC-USDT-SWAP'
    b.instr[inst] = dict(SPEC, instId=inst)
    before = b.st['positions'][inst]['contracts']
    x.fill_fraction = .5
    result = P.execute_action(b, {'action': 'close', 'instId': inst, 'reason': 'reduce'},
                              {'size': .4}, 'partial-swap')
    assert abs(result['remaining'] - (before - .2)) < 1e-9
    assert sum(float(a['sz']) for a in x.algos if a.get('slTriggerPx')) >= result['remaining']


def t_recovery_does_not_resurrect_emergency_closed_position():
    p, s, st, path = fixture()
    x = Fake()
    from okxbot.okx_client import OKXError
    def fail(body): raise OKXError('stop unavailable')
    x.place_algo = fail
    assert P.run_plan(p, True, client=x)['actions'][0]['status'] == 'failed'
    orders = len([1 for k, _ in x.posts if k == 'order'])
    b = P.make_bot(x); b.pos_mode = 'net_mode'
    assert P.recover_plan_orders(b)
    assert not b.st['spot']['longs']
    assert len([1 for k, _ in x.posts if k == 'order']) == orders



def t_spot_dust_does_not_count_as_position():
    plan, snapshot, state, path = fixture(opening(instId='BTC-USDT-SWAP', market='swap', size={'risk_usdt': 10}))
    snapshot['account']['positions'] = [
        {'instId': c + '-USDT', 'market': 'spot', 'side': 'long', 'size': 1e-6, 'last': 1.0} for c in ('ZRO', 'ONDO', 'APT', 'FIL')]
    order, _ = P.validate_action(plan['actions'][0], snapshot, state)
    assert order['instId'] == 'BTC-USDT-SWAP'
    snapshot['account']['positions'] = [
        {'instId': c + '-USDT', 'market': 'spot', 'side': 'long', 'size': 100, 'last': 10.0} for c in ('ZRO', 'ONDO', 'APT')]
    try:
        P.validate_action(plan['actions'][0], snapshot, state); raise AssertionError('expected MAX_POSITIONS')
    except ValueError as e:
        assert 'MAX_POSITIONS' in str(e)


def t_refresh_values_dust_at_snapshot_last_not_avg_px():
    # 10/20: execution-time refresh rebuilt spot balances with last=None, so ZRO/ONDO dust was valued
    # at a bogus accAvgPx and filled MAX_POSITIONS together with one real swap.
    plan, snapshot, state, path = fixture(opening(instId='BTC-USDT-SWAP', market='swap', side='short',
                                                  stop=103., take_profit=94., size={'risk_usdt': 10}))
    snapshot['account']['positions'] = [
        {'instId': 'ZRO-USDT', 'market': 'spot', 'side': 'long', 'size': 0.000344, 'avg_px': 170352.0, 'last': 1.869},
        {'instId': 'ONDO-USDT', 'market': 'spot', 'side': 'long', 'size': 0.000372, 'avg_px': 1432074.0, 'last': 0.437}]
    x = Fake()
    base_balance = x.balance
    def balance():
        rows = base_balance()
        rows[0]['details'] += [{'ccy': 'ZRO', 'cashBal': '0.000344', 'accAvgPx': '170352'},
                               {'ccy': 'ONDO', 'cashBal': '0.000372', 'accAvgPx': '1432074'},
                               {'ccy': 'APT', 'cashBal': '0.0000375', 'accAvgPx': '9999999'}]
        return rows
    x.balance = balance
    x.positions = lambda: [{'instId': 'ETH-USDT-SWAP', 'pos': '-1', 'avgPx': '2400', 'last': '2400', 'markPx': '2400'}]
    b = P.make_bot(x)
    out = P.refresh(b, snapshot, plan['actions'][0])
    last = {p['instId']: p['last'] for p in out['account']['positions']}
    assert last['ZRO-USDT'] == 1.869 and last['ONDO-USDT'] == 0.437, last
    assert last['APT-USDT'] == 100.0, last   # not in snapshot: read-only ticker fallback
    order, _ = P.validate_action(plan['actions'][0], out, P.load_state())
    assert order['instId'] == 'BTC-USDT-SWAP'


def t_definite_rejection_clears_pending_ambiguous_keeps_it():
    from okxbot.okx_client import OKXError
    cases = [
        (lambda b: OKXError('code=1', code='1', data=[{'sCode': '51155', 'sMsg': 'compliance'}]), False),
        (lambda b: OKXError('code=51008', code='51008'), False),
        (lambda b: [{'ordId': '', 'sCode': '51155', 'sMsg': 'compliance'}], False),
        (lambda b: OKXError('code=50004 timeout', code='50004'), True),
        (lambda b: OKXError('code=1', code='1', data=[{'sCode': '50013'}]), True),
        (lambda b: OKXError('POST /api/v5/trade/order outcome unknown after one retry: reset'), True),
        (lambda b: RuntimeError('connection reset'), True),
        (lambda b: [], True),
    ]
    for make, stays in cases:
        p, s, st, path = fixture()
        x = Fake()
        def place(body, make=make):
            x.posts.append(('order', copy.deepcopy(body)))
            r = make(body)
            if isinstance(r, Exception):
                raise r
            return r
        x.place_order = place
        result = P.run_plan(p, True, client=x)
        assert result['actions'][0]['status'] == 'failed', result
        assert bool(P.load_state().get('plan_pending')) == stays, (make(None), P.load_state().get('plan_pending'))
    # After a definite rejection, a later entry is no longer blocked by a stale intent.
    p, s, st, path = fixture()
    x = Fake(); orig = x.place_order
    x.place_order = lambda body: (_ for _ in ()).throw(OKXError('rej', code='1', data=[{'sCode': '51155'}]))
    assert P.run_plan(p, True, client=x)['actions'][0]['status'] == 'failed'
    x.place_order = orig
    p['plan_id'] = 'retry-after-reject'
    assert P.run_plan(p, True, client=x)['actions'][0]['status'] == 'executed'


def t_removed_instrument_cannot_open():
    assert 'XAG' not in config.TRADFI_LIST and 'XAG' not in config.TRADFI_COMMODITIES
    from okxbot.monitor.settings import DEFAULTS as M
    assert 'XAG' not in M['commodities']
    plan, snapshot, state, path = fixture(opening(instId='XAG-USDT-SWAP', market='swap', side='short',
                                                  stop=103., take_profit=94., size={'risk_usdt': 10}))
    snapshot['instruments']['XAG-USDT-SWAP'] = dict(snapshot['instruments']['BTC-USDT-SWAP'],
                                                    spec=dict(SPEC, instId='XAG-USDT-SWAP'))
    try:
        P.validate_action(plan['actions'][0], snapshot, state); raise AssertionError('expected removal block')
    except ValueError as e:
        assert 'removed from trading universe' in str(e), e

if __name__ == '__main__':
    for name, func in list(globals().items()):
        if name.startswith('t_'):
            func(); print('PASS', name)
