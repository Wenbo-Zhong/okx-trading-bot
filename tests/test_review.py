"""Decision review (Codex #6): structured review fields in the journal, outcome attribution, baselines."""
import datetime as dt, json, os, sys, tempfile
from pathlib import Path
os.environ.setdefault('OKXBOT_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from okxbot import config, plan as P, review as RV

NOW = dt.datetime.now(dt.timezone.utc)
H = 3600_000


def t_normalize_review():
    rv, err = RV.normalize_review({'category': 'news_event', 'hypothesis': ' CPI miss -> risk-on ', 'evidence': 'CPI 2.1%',
                                   'invalidation': 'BTC < 80k', 'horizon_h': 6, 'cost_budget_usdt': 12,
                                   'watch': ['ETH-USDT-SWAP', {'instId': 'BTC-USDT-SWAP', 'side': 'long', 'stop': 80000}]})
    assert not err and rv['hypothesis'] == 'CPI miss -> risk-on' and rv['evidence'] == ['CPI 2.1%']
    assert rv['watch'][1] == {'instId': 'BTC-USDT-SWAP', 'side': 'long', 'stop': 80000.0}
    rv, err = RV.normalize_review({'category': 'moon', 'horizon_h': 999, 'foo': 1, 'watch': [{'side': 'up'}]})
    assert len(err) == 4 and 'category' not in rv
    assert RV.normalize_review(None) == (None, []) and RV.normalize_review('x')[1]
    assert RV.infer_category('C7在ETH触发空头信号（12h破位）') == 'trend_breakout'
    assert RV.infer_category('CPI 超预期，新闻驱动') == 'news_event'
    assert RV.infer_category('mm_glitch 区间震荡') == 'oscillation'
    assert RV.infer_category('无明确优势') == 'other'


def t_no_trade_and_plan_journal_review():
    Path(config.DATA_DIR, 'decision_journal.jsonl').unlink(missing_ok=True)
    rec = P.record_no_trade('snap-x', '观望', review={'category': 'trend_breakout', 'horizon_h': 4,
                                                     'watch': [{'instId': 'BTC-USDT-SWAP', 'side': 'long'}]})
    assert rec['review']['horizon_h'] == 4 and rec['review_errors'] == []
    bad = P.record_no_trade('snap-x', '观望', review={'horizon_h': -1})
    assert bad['review_errors'] and bad['review'] == {}                  # still journaled
    sys.path.insert(0, str(Path(__file__).parent))
    import test_plan as TP
    plan, *_ = TP.fixture(TP.opening(review={'invalidation': 'close below 97', 'category': 'trend_breakout'}))
    plan['review'] = {'hypothesis': 'breakout continues', 'evidence': ['12h high broken'], 'horizon_h': 8,
                      'cost_budget_usdt': 2, 'category': 'other'}
    r = P.run_plan(plan)                                                 # dry run
    assert r['review']['hypothesis'] == 'breakout continues' and r['review_errors'] == []
    assert r['actions'][0]['review'] == {'invalidation': 'close below 97', 'category': 'trend_breakout'}
    assert r['actions'][0]['status'] == 'would_execute'                  # review never changes validation
    plan['review'] = {'horizon_h': 'soon'}; plan['plan_id'] = 'p2'
    r = P.run_plan(plan)
    assert r['review_errors'] and r['actions'][0]['status'] == 'would_execute'
    row = json.loads(Path(config.DATA_DIR, 'decision_journal.jsonl').read_text().splitlines()[-1])
    assert row['review_errors'] == r['review_errors']


def snap(d, sid, ts, last, bars, c7=None, inst='SOL-USDT-SWAP', equity=10000.0):
    s = {'snapshot_id': sid, 'generated_at': dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).isoformat(),
         'account': {'equity_usdt': equity},
         'instruments': {inst: {'last': last, 'c7': c7 or {'signal': 0},
                                'candles': {'1H': {'bars': bars, 'indicators': {'atr14': 1.0}}}},
                         'BTC-USDT-SWAP': {'last': 100, 'c7': {'signal': 0},
                                           'candles': {'1H': {'bars': [[b[0], 100, 101, 99, 100 + i] for i, b in enumerate(bars)]}}}}}
    Path(d, 'snapshots').mkdir(parents=True, exist_ok=True)
    Path(d, 'snapshots', sid + '.json').write_text(json.dumps(s))


def t_build_attribution_baselines_opportunity_cost():
    d = tempfile.mkdtemp()
    t0 = 1_800_000_000_000 - (1_800_000_000_000 % H)
    iso = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    bars0 = [[t0 - 2 * H, 100, 101, 99, 100], [t0 - H, 100, 101, 99, 100]]
    # snapshot 1: C7 long SOL at 100, stop 95, tp 110; planner says no-trade but watches SOL long, 2h horizon
    snap(d, 'snap-s1', t0 + 60_000, 100.0, bars0, {'signal': 1, 'stop': 95, 'tp': 110})
    # snapshot 2: later bars: first (entry) bar touches 111 (TP not credited), next bar hits TP
    bars1 = bars0 + [[t0, 100, 111, 99.5, 104], [t0 + H, 104, 112, 103, 108], [t0 + 2 * H, 108, 109, 107, 108]]
    snap(d, 'snap-s2', t0 + 3 * H + 60_000, 108.0, bars1)
    dj = [
        {'kind': 'no_trade', 'mode': 'no_trade', 'snapshot_id': 'snap-s1', 'ts': iso(t0 + 120_000), 'rationale': '观望',
         'review': {'category': 'trend_breakout', 'horizon_h': 2, 'watch': [{'instId': 'SOL-USDT-SWAP', 'side': 'long', 'stop': 95}]}},
        {'kind': 'plan', 'mode': 'execute', 'plan_id': 'P1', 'snapshot_id': 'snap-s1', 'ts': iso(t0 + 180_000),
         'rationale': 'CPI news', 'review': {'category': 'news_event', 'hypothesis': 'h'},
         'actions': [{'index': 0, 'action': 'open', 'status': 'executed', 'result': {'ordId': 'O1'}}],
         'outcome': {'order_ids': ['O1']}},
        {'kind': 'plan', 'mode': 'execute', 'plan_id': 'P2', 'snapshot_id': 'snap-s1', 'ts': iso(t0 + 200_000),
         'rationale': '区间震荡', 'actions': [{'index': 0, 'action': 'open', 'status': 'executed', 'result': {'ordId': 'O2'}}],
         'outcome': {'order_ids': ['O2']}},
    ]
    Path(d, 'decision_journal.jsonl').write_text('\n'.join(json.dumps(x) for x in dj) + '\n')
    ev = [{'ev': 'open', 'trade_id': 'O1', 'instId': 'BTC-USDT-SWAP', 'side': 'long', 'time_ms': t0 + 180_000,
           'entry': 100, 'init_stop': 95, 'planned_risk_usdt': 50, 'rationale': {'plan_id': 'P1'}},
          {'ev': 'close', 'trade_id': 'O1', 'time_ms': t0 + 2 * H, 'realized_pnl': '40', 'fee': '-2', 'funding': '-0.5'},
          {'ev': 'open', 'trade_id': 'O2', 'instId': 'ETH-USDT-SWAP', 'side': 'short', 'time_ms': t0 + 200_000,
           'entry': 100, 'init_stop': 102, 'planned_risk_usdt': 50, 'rationale': {'plan_id': 'P2'}},
          {'ev': 'close', 'trade_id': 'O2', 'time_ms': t0 + 2 * H, 'realized_pnl': '-25', 'remaining': 0},
          {'ev': 'open', 'trade_id': 'C1', 'instId': 'XRP-USDT-SWAP', 'side': 'long', 'time_ms': t0 - 10 * H,
           'entry': 1, 'init_stop': 0.95, 'planned_risk_usdt': 50, 'rationale': {'donchian_lookback_h': 12}},
          {'ev': 'close', 'trade_id': 'C1', 'time_ms': t0 - 5 * H, 'realized_pnl': '-50'}]
    Path(d, 'journal_events.jsonl').write_text('\n'.join(json.dumps(x) for x in ev) + '\n')
    res = RV.build(data_dir=d, now_ms=t0 + 4 * H)
    by = RV.summarize(res, 'planner')
    assert by['news_event']['net'] == 40 and by['news_event']['r'] == [0.8]
    assert by['oscillation']['net'] == -25 and by['oscillation']['inferred'] == 1
    assert RV.summarize(res, 'c7_auto')['trend_breakout']['net'] == -50       # pre-planner C7 trade kept apart
    sh = res['shadow']
    assert len(sh) == 1 and sh[0]['why'] == 'tp' and sh[0]['exit'] == 110 and sh[0]['exit_ts'] == t0 + H
    assert abs(sh[0]['net'] - (10 * 10 - 10 * 210 * (RV.FEE + RV.SLIP))) < 1e-6   # 0.5% of 10k / 5 = 10 units
    nt = res['no_trade'][0]
    assert nt['c7_available'] == 1 and nt['c7_shadow_net'] == sh[0]['net']
    w = nt['watch'][0]
    assert abs(w['fwd_return'] - 0.08) < 1e-9 and abs(w['fwd_r'] - 1.6) < 1e-9   # 100 -> 108 over 2h, stop 5%
    assert res['hold'] and res['hold']['btc_return'] > 0
    from scripts import review_report
    txt = review_report.render(res, True)
    assert 'C7 shadow' in txt and 'news_event' in txt and 'Cash' in txt and 'opportunity cost' in txt


if __name__ == '__main__':
    for name, func in list(globals().items()):
        if name.startswith('t_'):
            func(); print('PASS', name)
