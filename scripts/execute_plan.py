"""Validate a structured plan (okxbot.plan/v1); DRY-RUN by default, orders only with --execute (DEMO keys).

  python3 -m scripts.execute_plan PLAN.json [--execute] [--snapshot PATH]
  python3 -m scripts.execute_plan --no-trade --snapshot-id SNAP_ID --reason "..." [--uncertainty "..."]...
Prints one JSON result; every call is appended to DATA_DIR/decision_journal.jsonl."""
import argparse
import contextlib
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from okxbot.plan import run_plan, journal_decision, decision_record, record_no_trade


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', nargs='?')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--snapshot')
    parser.add_argument('--no-trade', action='store_true', help='journal an explicit no-trade decision')
    parser.add_argument('--snapshot-id')
    parser.add_argument('--reason')
    parser.add_argument('--uncertainty', action='append', default=[])
    args = parser.parse_args()
    if args.no_trade:
        if args.plan or args.execute or not args.snapshot_id or not args.reason:
            parser.error('--no-trade needs --snapshot-id and --reason (and no plan/--execute)')
        rec = record_no_trade(args.snapshot_id, args.reason, args.uncertainty)
        print(json.dumps(rec, sort_keys=True, ensure_ascii=False, allow_nan=False))
        return 0 if not rec['errors'] else 1
    if not args.plan:
        parser.error('PLAN.json required (or use --no-trade)')
    try:
        with open(args.plan, encoding='utf-8') as f:
            plan = json.load(f)
        with contextlib.redirect_stdout(sys.stderr):
            result = run_plan(plan, args.execute, args.snapshot)
    except Exception as e:
        result = {'plan_id': None, 'snapshot_id': None, 'mode': 'execute' if args.execute else 'dry_run',
                  'valid': False, 'plan_errors': [str(e)], 'actions': []}
        result = decision_record(result)
        journal_decision(result)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0 if result['valid'] and not any(a['status'] in ('failed', 'rejected') for a in result['actions']) else 1


if __name__ == '__main__':
    sys.exit(main())
