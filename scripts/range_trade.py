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
    try:
        L = R.validate(a.inst, a.lower, a.upper, a.regime_id, a.lev)
    except ValueError as e:
        R.journal({'event': 'rejected', 'instId': a.inst, 'regime_id': a.regime_id, 'error': str(e)})
        print(json.dumps({'valid': False, 'error': str(e)}, ensure_ascii=False)); return 1
    c = OKX()
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
    try:
        while r.step():
            time.sleep(0.5)
    except KeyboardInterrupt:
        r.stop('interrupted')
    except Exception as e:
        r.log('error', error=str(e)[:300]); r.stop('runner error')
    print(json.dumps({'stopped': r.done, 'trades': r.trades, 'cum_realized_est': round(r.cum, 2)}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
