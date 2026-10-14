"""Collect read-only planner inputs: python3 -m scripts.snapshot --help."""
import argparse
import json
from okxbot.okx_client import OKX
from okxbot.snapshot import build_snapshot, render_view, write_snapshot


def brief(snapshot):
    """Copy without raw bars: keeps last closed bar + indicators per timeframe. Same snapshot_id/sha256."""
    import copy
    out = copy.deepcopy(snapshot)
    for data in (out.get("instruments") or {}).values():
        for tf, c in (data.get("candles") or {}).items():
            if c and c.get("bars"):
                c["last_bar"] = c["bars"][-1]
                c["n_bars"] = len(c["bars"])
                del c["bars"]
    out["brief"] = True
    return out


def main(argv=None, client=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inst", nargs="+", help="override the default instrument universe")
    parser.add_argument("--no-news", action="store_true")
    parser.add_argument("--view", action="store_true")
    parser.add_argument("--out", help="also write snapshot JSON to this path")
    parser.add_argument("--brief", action="store_true",
                        help="stdout JSON without raw candle bars (indicators kept); saved file stays complete")
    args = parser.parse_args(argv)
    snapshot = build_snapshot(client if client is not None else OKX(), inst=args.inst, no_news=args.no_news)
    write_snapshot(snapshot, args.out)
    if args.view:
        print(render_view(snapshot))
    else:
        print(json.dumps(brief(snapshot) if args.brief else snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
