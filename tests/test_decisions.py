"""Decision journal records + no-trade + scripts.decisions. Run: python3 tests/test_decisions.py"""
import contextlib, io, json, os, sys, tempfile
from pathlib import Path
os.environ.setdefault('OKXBOT_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from okxbot import config, plan as P
import test_plan as TP

JOURNAL = Path(config.DATA_DIR, 'decision_journal.jsonl')


def rows():
    return [json.loads(l) for l in JOURNAL.read_text().splitlines() if l.strip()]


def t_dry_run_record_has_hashes():
    plan, snapshot, _, path = TP.fixture()
    snapshot['sha256'] = 'abc123'
    path.write_text(json.dumps(snapshot))
    P.run_plan(plan)
    r = rows()[-1]
    for k in ('ts', 'plan_id', 'snapshot_id', 'snapshot_sha256', 'plan_sha256', 'mode', 'valid',
              'rationale', 'uncertainties', 'actions', 'outcome', 'errors'):
        assert k in r, k
    assert r['snapshot_sha256'] == 'abc123'
    assert r['plan_sha256'] == P.canonical_sha256(plan)
    assert r['rationale'] == plan['rationale'] and r['mode'] == 'dry_run'
    assert r['outcome']['traded'] is False


def t_expired_plan_journaled_no_trade():
    import datetime as dt
    plan, *_ = TP.fixture()
    plan['valid_until'] = (TP.NOW - dt.timedelta(seconds=1)).isoformat()
    plan['created_at'] = (TP.NOW - dt.timedelta(minutes=5)).isoformat()
    res = P.run_plan(plan)
    assert not res['valid'] and 'plan expired' in res['plan_errors']
    r = rows()[-1]
    assert all(a['status'] == 'rejected' for a in r['actions']) and r['errors']


def t_no_trade_record():
    TP.fixture()
    rec = P.record_no_trade('test-snapshot', 'news too thin', ['FOMC tonight'])
    r = rows()[-1]
    assert r['kind'] == 'no_trade' and r['mode'] == 'no_trade'
    assert r['rationale'] == 'news too thin' and r['uncertainties'] == ['FOMC tonight']
    assert r['snapshot_id'] == 'test-snapshot' and r['snapshot_sha256']
    missing = P.record_no_trade('nope', 'x')
    assert missing['errors']


def t_decisions_script_output_and_outcome_join():
    TP.fixture()
    rec = P.decision_record({'plan_id': 'p1', 'snapshot_id': 's', 'mode': 'execute', 'valid': True,
                             'plan_errors': [], 'actions': [{'index': 0, 'action': 'open', 'instId': 'X-USDT',
                                                              'status': 'executed', 'reasons': [],
                                                              'result': {'ordId': 'O1', 'filled': 1}}]})
    P.journal_decision(rec)
    ev = Path(config.DATA_DIR, 'journal_events.jsonl')
    ev.write_text(json.dumps({'ev': 'close', 'trade_id': 'O1', 'realized_pnl': -2.5, 'remaining': 0}) + '\n')
    from scripts import decisions
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        decisions.main(['--json', '--last', '5'])
    data = json.loads(out.getvalue())
    hit = [d for d in data if d.get('plan_id') == 'p1'][0]
    assert hit['trade_outcomes']['O1'] == {'closes': 1, 'realized_pnl': -2.5, 'closed': True}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        decisions.main([])
    assert 'p1' in out.getvalue() and 'order O1' in out.getvalue()


def t_cli_no_trade():
    import subprocess
    TP.fixture()
    root = Path(__file__).resolve().parents[1]
    p = subprocess.run([sys.executable, '-m', 'scripts.execute_plan', '--no-trade', '--snapshot-id', 'test-snapshot',
                        '--reason', 'insufficient info'], cwd=root, capture_output=True, text=True,
                       env={**os.environ, 'OKXBOT_DATA_DIR': config.DATA_DIR})
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)['kind'] == 'no_trade'


if __name__ == '__main__':
    n = 0
    for k, f in list(globals().items()):
        if k.startswith('t_'):
            f(); n += 1; print('PASS', k)
    print(f'{n} tests passed')
