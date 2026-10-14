"""Validate a structured plan; orders require explicit --execute and DEMO keys."""
import argparse
import contextlib
import json
import sys
from okxbot.plan import run_plan, journal_decision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--snapshot')
    args = parser.parse_args()
    try:
        with open(args.plan, encoding='utf-8') as f:
            plan = json.load(f)
        with contextlib.redirect_stdout(sys.stderr):
            result = run_plan(plan, args.execute, args.snapshot)
    except Exception as e:
        result = {'plan_id': None, 'snapshot_id': None, 'mode': 'execute' if args.execute else 'dry_run',
                  'valid': False, 'plan_errors': [str(e)], 'actions': []}
        journal_decision(result)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0 if result['valid'] and not any(a['status'] in ('failed', 'rejected') for a in result['actions']) else 1


if __name__ == '__main__':
    sys.exit(main())
