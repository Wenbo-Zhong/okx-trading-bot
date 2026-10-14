"""Automatic safety maintenance cycle (planner mode). Run: python3 -m scripts.maintain [--dry-run]

Does ONLY: reconcile exchange-closed positions, recover pending order intents (spot + planner),
account circuit breaker (daily loss / max drawdown -> flatten + halt), verify/fill exchange stop
coverage, protect partial-fill remainders, log equity, refresh the journal, print `ALERTS:`.
Never opens positions, never trails/nudges stops, never exits on trend. Trade decisions come from
explicit plans (python3 -m scripts.execute_plan). Without API keys (or with --dry-run) it is read-only.
Exit codes: 0 ok, 1 cycle logged errors, 2 crash, 3 another instance holds data/run_once.lock."""
import fcntl, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import traceback
from okxbot import config
from okxbot.bot import Bot
from okxbot import alerts


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--dry-run" in argv:   # no keys -> public data only, no private calls, no orders
        for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"):
            os.environ.pop(k, None)
    assert config.DEMO, "Live (non-demo) trading is disabled in this build."
    os.makedirs(config.DATA_DIR, exist_ok=True)
    # Keep this descriptor open until process exit, including alerts/journal refresh.
    run_lock = open(os.path.join(config.DATA_DIR, "run_once.lock"), "a")
    try:
        fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        msg = "Another run_once instance is running; skipping trading."
        print(msg)
        alerts.print_section([{"type": "error", "msg": msg}])
        return 3
    code = 0
    b = None
    try:
        b = Bot()
        log = b.run()
        code = 1 if log and log.get("errors") else 0
    except Exception:
        traceback.print_exc()
        code = 2
    try:
        positions = []
        if b is not None and b.live:
            positions = [p for p in b.x.positions() if float(p.get("pos") or 0) != 0]
        hi = (b.log.get("news") or {}).get("high_impact", []) if b is not None else []
        fr = getattr(b, "funding", None) or None
        al = alerts.build(b.st if b is not None else {}, positions, hi, fr)
    except Exception as e:
        al = [{"type": "error", "msg": f"alerts failed: {e}"}]
    if b is not None:   # stop adjustments made this cycle are always reported (not subject to cooldown)
        for a in b.log.get("stop_adjustments", []):
            al.append({"type": "stop_adjust", "msg": f"{a['instId']} stop moved {a['exchange_old']:g} -> {a['exchange_new']:g} "
                       f"beyond est. liq cluster {a['clusters'][0][0]:g}-{a['clusters'][-1][1]:g}; max loss now "
                       f"{a['risk_ratio']}x original planned risk (amended + verified on exchange)"})
        for a in b.log.get("stop_adjustments_rejected", []):
            al.append({"type": "stop_adjust", "msg": f"{a['instId']} stop {a['old_stop']:g} sits near est. liq cluster but was KEPT: {a['reason']}"})
    alerts.print_section(al)
    try:   # refresh trade journal (read-only; never affects trading or the exit code)
        from okxbot import journal
        journal.build()
    except Exception as e:
        print(f"(journal refresh failed: {e})")
    return code


if __name__ == "__main__":
    sys.exit(main())
