"""Explicit planner instructions. No signal generation or discretionary trade moves."""
import copy
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid
from contextlib import contextmanager

from okxbot import config, sizing, journal_log as J
from okxbot.bot import Bot, clamp_sl_trigger, fmt_px, load_state, save_state
from okxbot.risk import guardrail_status
from okxbot.spot import SpotManager

UTC = dt.timezone.utc
TERMINAL = ('filled', 'canceled', 'mmp_canceled')


def journal_decision(record):
    path = Path(config.DATA_DIR, 'decision_journal.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False)
    with path.open('a', encoding='utf-8') as f:
        f.write(line + '\n')
        f.flush()
        os.fsync(f.fileno())


def canonical_sha256(obj):
    data = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False, default=str)
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def snapshot_sha256(snapshot_id=None, snapshot_path=None):
    """sha256 recorded inside the snapshot file (falls back to hashing the file)."""
    try:
        path = Path(snapshot_path) if snapshot_path else Path(config.DATA_DIR, 'snapshots', str(snapshot_id) + '.json')
        if snapshot_path is None and Path(str(snapshot_id)).name != str(snapshot_id):
            return None
        raw = path.read_bytes()
        try:
            return json.loads(raw).get('sha256') or hashlib.sha256(raw).hexdigest()
        except ValueError:
            return hashlib.sha256(raw).hexdigest()
    except (OSError, TypeError):
        return None


def outcome_summary(actions):
    counts = {}
    for a in actions:
        counts[a.get('status')] = counts.get(a.get('status'), 0) + 1
    orders = []
    for a in actions:
        r = a.get('result') or {}
        for k in ('ordId',):
            if r.get(k):
                orders.append(r[k])
    traded = any(a.get('status') == 'executed' and a.get('action') != 'hold' for a in actions)
    return {'counts': counts, 'order_ids': orders, 'traded': traded}


def decision_record(result, plan=None, snapshot_path=None, now=None):
    """Full review record: inputs (snapshot id + hash, plan hash), rationale, outcome."""
    plan = plan if isinstance(plan, dict) else {}
    rec = dict(result)
    rec['ts'] = iso_now(now)
    rec['kind'] = rec.get('kind') or 'plan'
    rec['plan_sha256'] = canonical_sha256(plan) if plan else None
    rec['snapshot_sha256'] = snapshot_sha256(rec.get('snapshot_id'), snapshot_path)
    rec['rationale'] = plan.get('rationale') if isinstance(plan.get('rationale'), str) else rec.get('rationale')
    unc = plan.get('uncertainties')
    rec['uncertainties'] = unc if isinstance(unc, list) else rec.get('uncertainties', [])
    rec['valid_until'] = plan.get('valid_until')
    rec['outcome'] = outcome_summary(rec.get('actions') or [])
    rec['errors'] = list(rec.get('plan_errors') or []) + [
        f"action {a.get('index')}: {'; '.join(a.get('reasons') or [])}"
        for a in rec.get('actions') or [] if a.get('status') in ('failed', 'rejected')]
    return rec


def iso_now(now=None):
    return (now or dt.datetime.now(UTC)).astimezone(UTC).isoformat().replace('+00:00', 'Z')


def record_no_trade(snapshot_id, reason, uncertainties=None, now=None):
    """Journal an explicit planner decision not to trade (insufficient info, nothing worth doing...)."""
    rec = decision_record({'kind': 'no_trade', 'plan_id': None, 'snapshot_id': snapshot_id, 'mode': 'no_trade',
                           'valid': True, 'plan_errors': [], 'actions': [],
                           'rationale': required_text(reason, 'reason'),
                           'uncertainties': list(uncertainties or [])}, None, None, now)
    if rec['snapshot_sha256'] is None:
        rec['errors'].append('snapshot not found: ' + str(snapshot_id))
    journal_decision(rec)
    return rec


@contextmanager
def instance_lock(timeout=60):
    Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
    with open(Path(config.DATA_DIR, 'run_once.lock'), 'a') as f:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('run_once.lock busy after waiting up to 60s')
                time.sleep(min(.1, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('timestamp must be ISO8601 with timezone')
    value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.utcoffset() is None:
        raise ValueError('timestamp requires timezone')
    return value


def positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError('expected finite positive number')
    return float(value)


def required_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + ' must be a non-empty string')
    return value


def validate_plan(plan, snapshot_path=None, now=None):
    now = now or dt.datetime.now(UTC)
    errors, snapshot = [], None
    try:
        if not isinstance(plan, dict) or plan.get('schema') != 'okxbot.plan/v1':
            raise ValueError('schema must be okxbot.plan/v1')
        for key in ('plan_id', 'snapshot_id', 'rationale'):
            required_text(plan.get(key), key)
        if not isinstance(plan.get('uncertainties'), list) or not all(isinstance(s, str) for s in plan['uncertainties']):
            raise ValueError('uncertainties must be a list of strings')
        created, expires = timestamp(plan.get('created_at')), timestamp(plan.get('valid_until'))
        if created > now:
            errors.append('created_at is in the future')
        if now > expires:
            errors.append('plan expired')
        if not dt.timedelta(0) <= expires - created <= dt.timedelta(hours=6):
            errors.append('validity window must be between 0 and 6 hours')
        if not isinstance(plan.get('actions'), list) or len(plan['actions']) > config.PLAN_MAX_ACTIONS:
            errors.append('actions must be a list with at most PLAN_MAX_ACTIONS entries')
        sid = plan['snapshot_id']
        if snapshot_path is None and (Path(sid).name != sid or sid in ('.', '..')):
            raise ValueError('invalid snapshot_id')
        path = Path(snapshot_path) if snapshot_path else Path(config.DATA_DIR, 'snapshots', sid + '.json')
        snapshot = json.loads(path.read_text(encoding='utf-8'))
        if snapshot.get('snapshot_id') != sid:
            errors.append('snapshot_id does not match snapshot file')
        age = now - timestamp(snapshot.get('generated_at'))
        if not dt.timedelta(0) <= age <= dt.timedelta(minutes=config.PLAN_MAX_SNAPSHOT_AGE_MIN):
            errors.append('snapshot stale or generated in the future')
        journal = Path(config.DATA_DIR, 'decision_journal.jsonl')
        if journal.exists():
            for line in journal.read_text(encoding='utf-8').splitlines():
                row = json.loads(line)  # corrupted journal fails closed
                if row.get('plan_id') == plan['plan_id'] and row.get('mode') == 'execute' and (row.get('claimed') or any(a.get('status') in ('executed', 'failed') for a in row.get('actions', []))):
                    errors.append('plan_id already executed or execution started')
                    break
    except (ValueError, TypeError, OSError, KeyError, AttributeError) as e:
        errors.append(str(e))
    return errors, snapshot


def position(state, inst):
    return (state.get('positions', {}).get(inst) if inst.endswith('-SWAP') else
            state.get('spot', {}).get('longs', {}).get(inst))


def validate_action(a, snapshot, state, reserved=None):
    """Return an order proposal and notes; errors raise ValueError. Pure/read-only."""
    reserved = reserved or set()
    if not isinstance(a, dict):
        raise ValueError('action must be an object')
    kind = a.get('action')
    if kind not in ('open', 'close', 'amend_sl', 'amend_tp', 'cancel', 'hold'):
        raise ValueError('unknown action')
    if 'rationale' in a and not isinstance(a['rationale'], str):
        raise ValueError('action rationale must be a string')
    if kind == 'hold':
        required_text(a.get('reason'), 'reason')
        if 'instId' in a:
            required_text(a['instId'], 'instId')
        return None, []
    inst = required_text(a.get('instId'), 'instId')
    account = snapshot.get('account') or {}
    data = (snapshot.get('instruments') or {}).get(inst) or {}
    rec = position(state, inst)
    positions = account.get('positions') or []
    held = next((p for p in positions if p['instId'] == inst), None)
    if kind == 'cancel':
        ids = [k for k in ('ordId', 'clOrdId', 'algoId') if k in a]
        if len(ids) != 1:
            raise ValueError('cancel requires exactly one order identifier')
        key = ids[0]
        required_text(a[key], key)
        rows = account.get('pending_algos' if key == 'algoId' else 'pending_orders') or []
        if not any(r.get(key) == a[key] and r.get('instId') == inst for r in rows):
            raise ValueError('order does not belong to this instrument')
        return {'instId': inst, key: a[key]}, []
    if kind != 'open' and not (rec and held):
        raise ValueError('tracked position must exist on exchange')
    if kind == 'close':
        required_text(a.get('reason'), 'reason')
        if 'fraction' in a and 'size' in a:
            raise ValueError('close requires fraction OR size')
        total = min(positive(held['size']), positive(rec.get('qty', rec.get('contracts'))))
        if 'size' in a:
            qty = positive(a['size'])
            if qty > total:
                raise ValueError('close size exceeds position')
        else:
            fraction = positive(a.get('fraction', 1))
            if fraction > 1:
                raise ValueError('fraction exceeds 1')
            qty = total * fraction
        spec = data.get('spec') or rec.get('spec') or {}
        qty = sizing.floor_step(qty, positive(float(spec['lotSz'])))
        if qty < float(spec['minSz']):
            raise ValueError('close below minimum size')
        return {'instId': inst, 'size': qty}, []
    px = positive(data.get('last'))
    spec = data.get('spec') or {}
    tick = positive(float(spec['tickSz']))
    side = (1 if a.get('side') == 'long' else -1) if kind == 'open' else (1 if held['side'] == 'long' else -1)
    key = 'take_profit' if kind == 'amend_tp' else 'stop'
    if kind == 'amend_tp' and key not in a:
        raise ValueError('take_profit is required (null removes it)')
    target = a.get(key)
    if target is not None or kind != 'amend_tp':
        target = float(fmt_px(positive(target), tick))
        if target <= 0:
            raise ValueError('trigger rounds to zero')
        if key == 'stop':
            if clamp_sl_trigger(side, target, px, data.get('mark'))[1]:
                raise ValueError('stop crosses current price or trigger buffer')
            if abs(px - target) / px > config.MAX_STOP_PCT + 1e-12:
                raise ValueError('stop exceeds MAX_STOP_PCT')
        elif (target - px) * side <= 0:
            raise ValueError('take_profit must be on profit side')
    if kind in ('amend_sl', 'amend_tp'):
        return {'instId': inst, key: target}, []
    market = a.get('market')
    if market not in ('spot', 'swap') or (market == 'swap') != inst.endswith('-SWAP'):
        raise ValueError('market does not match instrument')
    if a.get('side') not in ('long', 'short') or (market == 'spot' and side != 1):
        raise ValueError('invalid side; spot is long only')
    if data.get('tradable') is False or spec.get('state', 'live') != 'live' or not spec:
        raise ValueError('instrument not tradable')
    if data.get('liquidity_ok') is not True:
        raise ValueError('liquidity filter failed or unavailable')
    equity = positive(account.get('equity_usdt'))
    if not config.DEMO_USE_FULL_EQUITY:
        equity = min(equity, config.LIVE_EQUITY_CAP)
    can, _, why = guardrail_status(copy.deepcopy(state), equity)
    if not can or snapshot.get('guardrails', {}).get('can_open') is not True:
        raise ValueError('guardrails prevent opening: ' + why)
    if state.get('plan_pending') or state.get('spot', {}).get('pending_orders'):
        raise ValueError('unresolved order intent blocks entries')
    existing = [p['instId'] for p in positions] + list(state.get('positions', {})) + list(state.get('spot', {}).get('longs', {}))
    existing += [p.get('instId', '') for p in account.get('pending_orders') or []]
    coin = inst.split('-')[0]
    if coin in reserved or any(i.split('-')[0] == coin for i in existing):
        raise ValueError('duplicate coin position or pending order')
    if len({i.split('-')[0] for i in existing} | reserved) >= config.MAX_POSITIONS:
        raise ValueError('MAX_POSITIONS reached')
    lev = a.get('leverage', 1)
    if type(lev) is not int or not 1 <= lev <= config.MAX_LEVERAGE or (market == 'spot' and lev != 1):
        raise ValueError('leverage exceeds allowed cap')
    max_lever = spec.get('maxLever') or spec.get('lever')
    if max_lever and lev > float(max_lever):
        raise ValueError('leverage exceeds instrument cap')
    order_type = a.get('order_type', 'market')
    if order_type not in ('market', 'limit'):
        raise ValueError('invalid order_type')
    entry = float(fmt_px(positive(a.get('limit_px')), tick)) if order_type == 'limit' else px
    stop = target
    distance = (entry - stop) * side
    if distance <= 0 or min(distance / entry, abs(px - stop) / px) < config.MIN_STOP_PCT - 1e-12:
        raise ValueError('stop wrong side or below MIN_STOP_PCT')
    if distance / entry > config.MAX_STOP_PCT + 1e-12:
        raise ValueError('entry stop distance exceeds MAX_STOP_PCT')
    tp = a.get('take_profit')
    if tp is not None:
        tp = float(fmt_px(positive(tp), tick))
        if (tp - px) * side <= 0 or (tp - entry) * side <= 0:
            raise ValueError('take_profit must be on profit side')
    size = a.get('size')
    if not isinstance(size, dict) or len(size) != 1 or next(iter(size)) not in ('risk_usdt', 'notional_usdt', 'contracts'):
        raise ValueError('size requires exactly one of risk_usdt/notional_usdt/contracts')
    units = float(spec.get('ctVal') or 1) * float(spec.get('ctMult') or 1) if market == 'swap' else 1
    cap = equity * (config.SPOT_RISK_PER_TRADE if market == 'spot' else config.RISK_PER_TRADE)
    key, value = next(iter(size.items()))
    value, notes = positive(value), []
    if key == 'risk_usdt':
        if value > cap:
            notes.append('risk_usdt clamped to per-trade cap')
        qty = min(value, cap) / distance / units
    else:
        qty = value / entry / units if key == 'notional_usdt' else value
        if qty * units * distance > cap + 1e-9:
            raise ValueError('risk exceeds per-trade cap')
    notional = qty * units * entry
    if notional / lev > equity * config.MAX_MARGIN_PCT + 1e-9:
        raise ValueError('margin exceeds MAX_MARGIN_PCT')
    if data.get('liquidity_cap') is not None:
        if data['liquidity_cap'] < notional:
            notes.append('size clamped by executing-venue liquidity')
        notional = min(notional, data['liquidity_cap'])
    if market == 'spot':
        amount_caps = [v for v in (data.get('depth_cap'), float(spec.get('maxMktAmt') or 0)) if v is not None and v > 0]
        if data.get('depth_cap') == 0:
            raise ValueError('spot depth cap is zero')
        notional, extra = sizing.clamp_spot(notional, positive(account.get('avail_usdt')), entry,
            data.get('exchange_max'), min(amount_caps) if amount_caps else None,
            float(spec['minSz']), config.SPOT_MIN_ORDER_USDT)
        qty = sizing.floor_step(notional / entry, float(spec['lotSz']))
    else:
        qty, extra = sizing.clamp_swap(notional / entry / units, float(spec['lotSz']), float(spec['minSz']),
            lev, units, entry, positive(account.get('avail_usdt')), data.get('exchange_max'),
            float(spec.get('maxLmtSz' if order_type == 'limit' else 'maxMktSz') or 0) or None)
    if qty < float(spec['minSz']) or qty <= 0:
        raise ValueError('size below minimum after clamps')
    notes += extra
    body = {'instId': inst, 'tdMode': 'cash' if market == 'spot' else config.TD_MODE,
            'side': 'buy' if side == 1 else 'sell', 'ordType': order_type, 'sz': str(qty)}
    if order_type == 'limit':
        body['px'] = str(entry)
    elif market == 'spot':
        body.update(sz=str(round(qty * entry, 2)), tgtCcy='quote_ccy')
    attach = {'slTriggerPx': str(stop), 'slOrdPx': '-1', 'slTriggerPxType': 'last' if market == 'spot' else 'mark'}
    if tp is not None:
        attach.update(tpTriggerPx=str(tp), tpOrdPx='-1', tpTriggerPxType='last')
    body['attachAlgoOrds'] = [attach]
    return body, notes


def make_bot(client):
    # Avoid Bot.account(): it can change account mode even during a preview.
    b = Bot.__new__(Bot)
    b.x, b.live, b.mode = client, True, 'demo'
    b.st, b.log = load_state(), {'events': [], 'errors': []}
    b.swaps_blocked, b.protection_failed = False, False
    b.instr, b.last_px = {}, {}
    b.spot = SpotManager(b)
    return b


def refresh(b, snapshot, action):
    """Fresh read-only account and executing-venue checks before each action."""
    from okxbot.snapshot import fetch_account
    out = copy.deepcopy(snapshot)
    errors = []
    out['account'] = fetch_account(b.x, b.st, errors)
    if errors:
        raise ValueError('account refresh incomplete: ' + '; '.join(errors))
    b.pos_mode = b.x.account_config()[0]['posMode']
    b.usdt_avail = out['account']['avail_usdt']
    can, _, why = guardrail_status(b.st, out['account']['equity_usdt'])
    out['guardrails'] = {'can_open': can and not b.protection_failed, 'status': why}
    save_state(b.st)
    if action.get('action') in ('hold', 'cancel'):
        return out
    inst = action['instId']
    market = 'swap' if inst.endswith('-SWAP') else 'spot'
    spec = b.x._req('GET', '/api/v5/public/instruments', {'instType': market.upper(), 'instId': inst}, demo_public=True)[0]
    px = float(b.x._req('GET', '/api/v5/market/ticker', {'instId': inst}, demo_public=True)[0]['last'])
    mark = None
    if market == 'swap':
        mark = float(b.x._req('GET', '/api/v5/public/mark-price', {'instType': 'SWAP', 'instId': inst}, demo_public=True)[0]['markPx'])
    data = {'spec': spec, 'last': px, 'mark': mark, 'tradable': spec.get('state') == 'live', 'liquidity_ok': True}
    out.setdefault('instruments', {})[inst] = data
    b.instr[inst], b.last_px[inst] = spec, px
    b.spot.inst[inst], b.spot.last[inst] = spec, px
    if action.get('action') == 'open':
        # First validate to obtain the proposed notional; then apply actual demo book clamps.
        body, _ = validate_action(action, out, b.st)
        entry = float(body.get('px') or px)
        units = float(spec.get('ctVal') or 1) * float(spec.get('ctMult') or 1) if market == 'swap' else 1
        notional = float(body['sz']) if body.get('tgtCcy') else float(body['sz']) * units * entry
        capped, ok, _ = b.liquidity_check(inst, 1 if action['side'] == 'long' else -1, entry,
                                         action['stop'], notional, units, market)
        data.update(liquidity_ok=ok, liquidity_cap=capped)
        ms = b.x.max_size(inst, body['tdMode'], body.get('px'))[0]
        data['exchange_max'] = float(ms['maxBuy' if action['side'] == 'long' else 'maxSell'])
        if market == 'spot':
            data['depth_cap'] = b.spot.depth_cap(inst)
    return out


def checked(rows):
    if not rows or any(r.get('sCode') not in (None, '', '0') for r in rows):
        raise ValueError('exchange rejected request: ' + str(rows))
    return rows


def settle(b, pending):
    inst, body = pending['body']['instId'], pending['body']
    if pending.get('ordId'):
        row = b.x.get_order(inst, pending['ordId'])[0]
    else:
        row = b.x._req('GET', '/api/v5/trade/order', {'instId': inst, 'clOrdId': body['clOrdId']}, private=True)[0]
        pending['ordId'] = row['ordId']
        save_state(b.st)
    if row.get('state') not in TERMINAL:
        for _ in range(4):
            time.sleep(.5)
            row = b.x.get_order(inst, pending['ordId'])[0]
            if row.get('state') in TERMINAL:
                break
    if row.get('state') not in TERMINAL:
        # Bounded lifetime: cancel unfilled limit/market remainder before releasing the lock.
        checked(b.x.cancel_order(inst, pending['ordId']))
        row = b.x.get_order(inst, pending['ordId'])[0]
        if row.get('state') not in TERMINAL:
            raise ValueError('order still pending; recovery required')
    return row


def ensure_tp(b, inst, side, qty, tp):
    """Install only the explicitly requested TP if attachment did not appear."""
    if tp is None:
        return
    exit_side = 'sell' if side == 1 else 'buy'
    matched = [a for a in b.x.pending_algos(inst) if a.get('side') == exit_side
               and a.get('posSide', 'net') in ('net', '', b.pos_side(side))
               and abs(float(a.get('tpTriggerPx') or 0) - tp) < 1e-9]
    missing = qty - sum(float(a.get('sz') or 0) for a in matched)
    if missing <= 1e-9:
        return
    body = {'instId': inst, 'tdMode': config.TD_MODE if inst.endswith('-SWAP') else 'cash',
            'side': exit_side, 'ordType': 'conditional', 'sz': str(missing),
            'tpTriggerPx': str(tp), 'tpOrdPx': '-1'}
    if inst.endswith('-SWAP'):
        body['reduceOnly'] = True
        if b.pos_side(side):
            body['posSide'] = b.pos_side(side)
    res = checked(b.x.place_algo(body))[0]
    for _ in range(4):
        if any(a.get('algoId') == res.get('algoId') and float(a.get('tpTriggerPx') or 0) == tp
               for a in b.x.pending_algos(inst)):
            return
        time.sleep(.5)
    raise ValueError('take profit placement not confirmed (stop retained)')


def finish_open(b, pending):
    row = settle(b, pending)
    inst, a, spec = pending['body']['instId'], pending['action'], pending['spec']
    qty, px = float(row.get('accFillSz') or 0), float(row.get('avgPx') or 0)
    if qty <= 0:
        return {'ordId': pending['ordId'], 'filled': 0}
    if px <= 0:
        raise ValueError('fill price unavailable')
    stop = float(pending['body']['attachAlgoOrds'][0]['slTriggerPx'])
    tp = a.get('take_profit')
    side = 1 if a['side'] == 'long' else -1
    spot = a['market'] == 'spot'
    units = 1 if spot else float(spec['ctVal']) * float(spec.get('ctMult') or 1)
    if spot and row.get('feeCcy') == inst.split('-')[0]:
        qty = sizing.floor_step(qty + float(row.get('fee') or 0), float(spec['lotSz']))
    records = b.spot.st['longs'] if spot else b.st.setdefault('positions', {})
    if inst in records and records[inst].get('ordId') != pending['ordId']:
        raise ValueError('recovered order conflicts with tracked position')
    if pending.get('recorded'):
        current = records.get(inst)
        if not current:
            return {'ordId': pending['ordId'], 'filled': qty, 'already_closed': True}
        qty = current.get('qty', current.get('contracts'))
        stop, tp = current['stop'], current.get('tp')
    else:
        records[inst] = {'side': side, 'entry': px, 'stop': stop, 'init_stop': stop, 'tp': tp,
                     'qty' if spot else 'contracts': qty, 'lev': a.get('leverage', 1),
                     'ordId': pending['ordId'], 'trade_id': pending['ordId'], 'spec': spec,
                     'entry_ts': int(time.time() * 1000), 'intent': 'planner',
                     'planned_risk_usdt': abs(px - stop) * qty * units}
    b.instr[inst] = b.spot.inst[inst] = spec
    b.spot.last[inst] = px
    was_recorded = pending.get('recorded', False)
    pending['recorded'] = True
    save_state(b.st)
    if not was_recorded:
        J.record('open', trade_id=pending['ordId'], instId=inst, market=a['market'], side=a['side'],
             entry=px, size=qty, ct_val=units, lev=a.get('leverage', 1), init_stop=stop, tp=tp,
             planned_risk_usdt=abs(px - stop) * qty * units, rationale={'plan_id': pending['plan_id']})
    if spot:
        try:
            b.spot.protect_long(inst)
        except Exception:
            b.spot.exit_long(inst, 'could not confirm stop coverage')
            raise
    elif b.ensure_sl_coverage(inst, side, qty, stop, b.pos_side(side), float(spec['lotSz'])) is None:
        raise ValueError('could not confirm stop coverage; emergency close requested')
    ensure_tp(b, inst, side, qty, float(fmt_px(tp, spec['tickSz'])) if tp is not None else None)
    return {'ordId': pending['ordId'], 'filled': qty, 'price': px}


def recover_plan_orders(b):
    """Called by maintenance and execution; never submits an entry again."""
    for key, pending in list(b.st.get('plan_pending', {}).items()):
        try:
            finish_open(b, pending)
            del b.st['plan_pending'][key]
            save_state(b.st)
        except Exception as e:
            b.protection_failed = True
            b.err(f'planner order recovery {key}: {e}')
    return not b.st.get('plan_pending')


def execute_action(b, a, order, plan_id):
    inst, kind = a.get('instId'), a['action']
    if kind == 'hold':
        return {'reason': a['reason']}
    if kind == 'cancel':
        if 'algoId' in order:
            return {'response': checked(b.x.cancel_algos([order]))}
        return {'response': checked(b.x.cancel_order(inst, order.get('ordId'), order.get('clOrdId')))}
    rec = position(b.st, inst)
    if kind == 'open':
        body = copy.deepcopy(order)
        body['clOrdId'] = 'botp' + uuid.uuid4().hex[:24]
        side = 1 if a['side'] == 'long' else -1
        if a['market'] == 'swap':
            pos_side = b.pos_side(side)
            if pos_side:
                body['posSide'] = pos_side
            checked(b.x.set_leverage(inst, a.get('leverage', 1), pos_side))
            # Recheck tier affordability at the explicitly requested leverage.
            ms = b.x.max_size(inst, config.TD_MODE, body.get('px'))[0]
            spec = b.instr[inst]
            qty, _ = sizing.clamp_swap(float(body['sz']), float(spec['lotSz']), float(spec['minSz']),
                a.get('leverage', 1), float(spec['ctVal']) * float(spec.get('ctMult') or 1),
                float(body.get('px') or b.last_px[inst]), b.usdt_avail,
                float(ms['maxBuy' if side == 1 else 'maxSell']))
            if qty <= 0:
                raise ValueError('size below minimum at requested leverage')
            body['sz'] = str(qty)
        order.update(body)
        pending = {'body': body, 'action': a, 'spec': b.instr[inst], 'plan_id': plan_id}
        b.st.setdefault('plan_pending', {})[body['clOrdId']] = pending
        save_state(b.st)  # write-ahead intent, including for swap and limit orders
        response = checked(b.x.place_order(body))[0]
        pending['ordId'] = response['ordId']
        save_state(b.st)
        result = finish_open(b, pending)
        del b.st['plan_pending'][body['clOrdId']]
        save_state(b.st)
        return result
    if kind == 'close':
        if not inst.endswith('-SWAP'):
            before = rec['qty']
            b.spot.exit_long(inst, a['reason'], size=order['size'])
            remaining = b.spot.st['longs'].get(inst, {}).get('qty', 0)
            if remaining:
                b.spot.protect_long(inst)
            if remaining >= before or b.protection_failed:
                raise ValueError('close failed or remainder protection failed')
            return {'filled': before - remaining, 'remaining': remaining}
        side = rec['side']
        body = {'instId': inst, 'tdMode': config.TD_MODE, 'side': 'sell' if side == 1 else 'buy',
                'ordType': 'market', 'sz': str(order['size']), 'reduceOnly': True,
                'clOrdId': 'botc' + uuid.uuid4().hex[:24]}
        if b.pos_side(side):
            body['posSide'] = b.pos_side(side)
        res = checked(b.x.place_order(body))[0]
        row = settle(b, {'body': body, 'ordId': res['ordId']})
        filled = float(row.get('accFillSz') or 0)
        rec['contracts'] = max(0, rec['contracts'] - filled)
        save_state(b.st)
        J.record('close', instId=inst, trade_id=rec.get('trade_id'), execution_id=f"{res['ordId']}:{filled:g}",
                 size=filled, remaining=rec['contracts'], exit=float(row.get('avgPx') or 0), reason=a['reason'])
        if rec['contracts']:
            if b.ensure_sl_coverage(inst, side, rec['contracts'], rec['stop'], b.pos_side(side),
                                    float(b.instr[inst]['lotSz'])) is None:
                raise ValueError('remainder stop coverage failed')
        else:
            b.cancel_algos(inst)
            b.st['positions'].pop(inst)
        save_state(b.st)
        if not filled:
            raise ValueError('close had no fills')
        return {'filled': filled, 'remaining': rec['contracts']}
    key, prefix = ('stop', 'Sl') if kind == 'amend_sl' else ('take_profit', 'Tp')
    value = order[key]
    algos = [r for r in b.x.pending_algos(inst) if r.get('side') == ('sell' if rec.get('side', 1) == 1 else 'buy')
             and r.get('posSide', 'net') in ('net', '', b.pos_side(rec.get('side', 1)))]
    field = prefix.lower() + 'TriggerPx'
    algos = [r for r in algos if r.get(field)]
    if kind == 'amend_tp' and not algos:
        ensure_tp(b, inst, rec.get('side', 1), rec.get('qty', rec.get('contracts')), value)
    elif not algos:
        raise ValueError('no protective algo available to amend')
    removed = set()
    for algo in algos:
        if kind == 'amend_tp' and value is None and not algo.get('slTriggerPx'):
            checked(b.x.cancel_algos([{'instId': inst, 'algoId': algo['algoId']}]))
            removed.add(algo['algoId'])
            continue
        checked(b.x.amend_algo({'instId': inst, 'algoId': algo['algoId'],
                'new' + prefix + 'TriggerPx': '0' if value is None else str(value),
                'new' + prefix + 'OrdPx': '-1' if value is not None else '0'}))
    live = b.x.pending_algos(inst)
    field = prefix.lower() + 'TriggerPx'
    for algo in algos:
        actual = next((r for r in live if r['algoId'] == algo['algoId']), None)
        if algo['algoId'] in removed:
            if actual is not None:
                raise ValueError('TP cancellation not confirmed')
            continue
        if actual is None or abs(float(actual.get(field) or 0) - (value or 0)) > 1e-9:
            raise ValueError('algo amendment not confirmed')
    rec['stop' if kind == 'amend_sl' else 'tp'] = value
    save_state(b.st)
    J.record('stop' if kind == 'amend_sl' else 'tp', instId=inst, trade_id=rec.get('trade_id'), value=value)
    return {'confirmed': True, key: value}


def run_plan(plan, execute=False, snapshot_path=None, client=None, now=None, lock_timeout=60):
    """Validate, optionally execute, and durably journal one JSON-serializable result."""
    result = {'plan_id': plan.get('plan_id') if isinstance(plan, dict) and isinstance(plan.get('plan_id'), str) else None,
              'snapshot_id': plan.get('snapshot_id') if isinstance(plan, dict) and isinstance(plan.get('snapshot_id'), str) else None,
              'mode': 'execute' if execute else 'dry_run', 'valid': False, 'plan_errors': [], 'actions': []}
    try:
        with instance_lock(lock_timeout):
            errors, snapshot = validate_plan(plan, snapshot_path, now)
            result['plan_errors'] = errors
            b = None
            if execute and not errors:
                if not config.DEMO:
                    errors.append('DEMO must be True')
                else:
                    from okxbot.okx_client import OKX
                    client = client or OKX()
                    if not client.has_keys:
                        errors.append('--execute requires API keys')
                    else:
                        assert config.DEMO
                        b = make_bot(client)
                        b.pos_mode = client.account_config()[0]['posMode']
                        recover_plan_orders(b)
            state = b.st if b else load_state()
            result['valid'] = not errors
            # Reserve ID before any action can POST: a crash cannot replay an entry.
            if execute and not errors:
                journal_decision({**result, 'claimed': True})
            reserved = set()
            actions = plan.get('actions', []) if isinstance(plan, dict) else []
            for index, a in enumerate(actions if isinstance(actions, list) else []):
                row = {'index': index, 'action': a.get('action') if isinstance(a, dict) and isinstance(a.get('action'), str) else None,
                       'instId': a.get('instId') if isinstance(a, dict) and isinstance(a.get('instId'), str) else None,
                       'status': 'rejected', 'reasons': [], 'order': None, 'result': None}
                result['actions'].append(row)
                if errors:
                    row['reasons'] = ['plan-level validation failed']
                    continue
                try:
                    current = refresh(b, snapshot, a) if b else snapshot
                    # Recheck expiry immediately before each action, including after slow reads.
                    if execute:
                        current_time = dt.datetime.now(UTC)
                        if current_time > timestamp(plan['valid_until']):
                            raise ValueError('plan expired before action')
                        if current_time - timestamp(snapshot['generated_at']) > dt.timedelta(minutes=config.PLAN_MAX_SNAPSHOT_AGE_MIN):
                            raise ValueError('snapshot stale before action')
                    order, notes = validate_action(a, current, state, reserved)
                    row.update(order=order, reasons=notes)
                except Exception as e:
                    row['reasons'].append(str(e))
                    continue
                if a['action'] == 'open':
                    reserved.add(a['instId'].split('-')[0])
                if not execute:
                    row['status'] = 'would_execute'
                    continue
                try:
                    row['result'] = execute_action(b, a, order, plan['plan_id'])
                    row['status'] = 'skipped' if a['action'] == 'hold' or (a['action'] == 'open' and not row['result'].get('filled')) else 'executed'
                except Exception as e:
                    row.update(status='failed', reasons=notes + [str(e)])
            result = decision_record(result, plan, snapshot_path, now)
            journal_decision(result)
    except Exception as e:
        result['valid'] = False
        result['plan_errors'].append(str(e))
        try:
            result = decision_record(result, plan, snapshot_path, now)
            journal_decision(result)
        except OSError:
            pass  # The original journal/storage failure is already reported.
    return result
