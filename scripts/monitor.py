"""Event monitor CLI.
  python3 -m scripts.monitor --watchlist [--force]   dry run: build + print watchlist, no WS
  python3 -m scripts.monitor                          run (log-only: events -> data/monitor_events.jsonl)
  python3 -m scripts.monitor --enable-wakes           also POST wakes to $OKX_WAKE_WEBHOOK_URL (needs env)"""
import argparse, json, logging, os, signal, sys, threading
from okxbot import config
from okxbot.monitor.service import Monitor

PID_F = os.path.join(config.DATA_DIR, "monitor.pid")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchlist", action="store_true")
    ap.add_argument("--force", action="store_true", help="rebuild alt rotation now")
    ap.add_argument("--enable-wakes", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    m = Monitor(enable_wakes=a.enable_wakes)
    if a.watchlist:
        wl = m.build_watchlist(force=a.force)
        groups = {}
        for inst, e in wl["instruments"].items():
            groups.setdefault(e["group"], []).append(f"{inst} {'/'.join(e['bars'])}{' [held]' if e.get('held') else ''}")
        print(json.dumps({"rotation": wl["rotation"], "groups": groups, "alts_rejected": wl["alts_rejected"]},
                         indent=1, ensure_ascii=False))
        return 0
    if a.enable_wakes and not os.environ.get("OKX_WAKE_WEBHOOK_URL"):
        print("--enable-wakes needs OKX_WAKE_WEBHOOK_URL", file=sys.stderr)
        return 2
    with open(PID_F, "w") as f:
        f.write(str(os.getpid()))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    logging.info("monitor start, wakes=%s", "webhook" if m.waker.enabled else "log-only")
    try:
        m.run(stop)
    finally:
        os.path.exists(PID_F) and os.remove(PID_F)
    return 0


if __name__ == "__main__":
    sys.exit(main())
