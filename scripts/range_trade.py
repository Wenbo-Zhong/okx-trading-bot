"""mm_glitch band trader (planner-invoked). DEMO only; DRY-RUN (log-only) unless
config.RANGE_TRADE_ENABLED is True AND --execute is given.

  python3 -m scripts.range_trade --inst ETH-USDT-SWAP --lower 2053.6 --upper 2086.3 --regime-id ETH-USDT-SWAP:1770480480000 [--lev 20] [--execute]

Validates the band against the confirmed regime in data/mm_glitch_state.json, then runs alongside the
monitor with a WS ticker feed. Every action is appended to data/decision_journal.jsonl (kind=range_trade)."""
import argparse, json, os, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import websocket
from okxbot import config, range_trade as R
from okxbot.okx_client import OKX


def ws_price(inst):
    box = {'px': None}

    def run():
        while True:
            ws = websocket.WebSocketApp('wss://ws.okx.com/ws/v5/public',
                on_open=lambda w: w.send(json.dumps({'op': 'subscribe', 'args': [{'channel': 'tickers', 'instId': inst}]})),
                on_message=lambda w, m: m != 'pong' and box.update(
                    px=float(json.loads(m)['data'][0]['last'])) if '"data"' in m else None)
            ws.run_forever(ping_interval=20, ping_timeout=10)
            box['px'] = None    # stale on disconnect -> runner idles
            time.sleep(1)
    threading.Thread(target=run, daemon=True).start()
    return lambda: box['px']


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--inst', required=True)
    ap.add_argument('--lower', type=float, required=True)
    ap.add_argument('--upper', type=float, required=True)
    ap.add_argument('--regime-id', required=True)
    ap.add_argument('--lev', type=int, default=config.MM_GLITCH_MAX_LEVERAGE)
    ap.add_argument('--execute', action='store_true')
    a = ap.parse_args()
    assert config.DEMO, 'DEMO only'
    c = OKX()
    costs, errs = {}, []
    try:   # actual fee rate + executing-book spread for the post-fee edge gate (config fallbacks on error)
        from okxbot.snapshot import fetch_fees
        costs['fee_taker'] = fetch_fees(c, a.inst, errs)['taker']
        t = c._req('GET', '/api/v5/market/ticker', {'instId': a.inst}, demo_public=True)[0]   # executing (demo) book
        bid, ask = float(t['bidPx']), float(t['askPx'])
        costs['spread_frac'] = (ask - bid) / ((ask + bid) / 2)
    except Exception as e:
        errs.append(str(e)[:200])
    try:
        L = R.validate(a.inst, a.lower, a.upper, a.regime_id, a.lev, costs=costs)
    except ValueError as e:
        R.journal({'event': 'rejected', 'instId': a.inst, 'regime_id': a.regime_id, 'error': str(e), 'costs': costs,
                   'cost_errors': errs})
        print(json.dumps({'valid': False, 'error': str(e)}, ensure_ascii=False)); return 1
    execute = a.execute and config.RANGE_TRADE_ENABLED
    if a.execute and not execute:
        print('RANGE_TRADE_ENABLED is False -> dry-run')
    if execute and not c.has_keys:
        print('--execute requires DEMO API keys'); return 1
    spec = c._req('GET', '/api/v5/public/instruments', {'instType': 'SWAP', 'instId': a.inst})[0]
    equity = float(c.balance()[0]['totalEq']) if c.has_keys else 10000.0
    if not config.DEMO_USE_FULL_EQUITY:
        equity = min(equity, config.LIVE_EQUITY_CAP)
    r = R.Runner(c, a.inst, a.lower, a.upper, a.regime_id, a.lev, execute=execute, price=ws_price(a.inst),
                 spec=spec, equity=equity)
    r.log('start', band=[a.lower, a.upper], levels=L, equity=equity)
    print(json.dumps({'valid': True, 'mode': 'execute' if r.execute else 'dry_run', 'levels': L}))
    give_up = None
    while True:
        try:
            if not r.step():
                break
        except KeyboardInterrupt:
            r.stop('interrupted')
        except Exception as e:
            r.log('error', error=str(e)[:300]); r.stop('runner error')
        if r.stopping and not r.done:      # flatten not confirmed: keep retrying, exchange stop stays on
            give_up = give_up or time.time() + config.RANGE_FLATTEN_RETRY_S
            if time.time() > give_up:
                r.log('flatten_abandoned', reason=r.stopping,
                      note='exchange SL/TP and monitor max-hold enforcement remain as backstop')
                break
            time.sleep(2)
            continue
        time.sleep(0.5)
    print(json.dumps({'stopped': r.done or r.stopping, 'trades': r.trades, 'cum_realized': round(r.cum, 2),
                      'mode': 'execute' if r.execute else 'dry_run (simulated)'}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
