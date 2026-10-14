"""Run one decision cycle. Without OKX_API_KEY/SECRET/PASSPHRASE env vars it runs as a
dry run (public data + decisions logged, no orders). With keys it trades on OKX DEMO only.
Ends with an `ALERTS:` section (empty if none) for the hourly routine to relay."""
import fcntl, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sys, traceback
from okxbot import config
from okxbot.bot import Bot
from okxbot import alerts

if __name__ == "__main__":
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
        sys.exit(3)
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
    sys.exit(code)
