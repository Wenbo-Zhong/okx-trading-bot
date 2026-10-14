"""Collect read-only planner inputs: python3 -m scripts.snapshot --help."""
import argparse
import json
from okxbot.okx_client import OKX
from okxbot.snapshot import build_snapshot, render_view, write_snapshot


def main(argv=None, client=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inst", nargs="+", help="override the default instrument universe")
    parser.add_argument("--no-news", action="store_true")
    parser.add_argument("--view", action="store_true")
    parser.add_argument("--out", help="also write snapshot JSON to this path")
    args = parser.parse_args(argv)
    snapshot = build_snapshot(client if client is not None else OKX(), inst=args.inst, no_news=args.no_news)
    write_snapshot(snapshot, args.out)
    print(render_view(snapshot) if args.view else json.dumps(snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
