"""Review recent planner decisions joined with trade outcomes.

  python3 -m scripts.decisions [--last N] [--json]
JSON: list of decision records (see okxbot.plan.decision_record) each with an added
"trade_outcomes": {ordId: {"closed": bool, "realized_pnl": float|null, "closes": n}}."""
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from okxbot import config


def load_jsonl(path):
    rows = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    rows.append({"kind": "corrupt_line", "raw": line[:200]})
    return rows


def outcomes(order_ids, events):
    out = {}
    for oid in order_ids:
        closes = [e for e in events if e.get("ev") == "close" and str(e.get("trade_id")) == str(oid)]
        pnl = [e.get("realized_pnl") for e in closes if isinstance(e.get("realized_pnl"), (int, float))]
        remaining = [e.get("remaining") for e in closes if "remaining" in e]
        out[oid] = {"closes": len(closes), "realized_pnl": round(sum(pnl), 6) if pnl else None,
                    "closed": bool(closes) and (not remaining or not remaining[-1])}
    return out


def recent(last=10):
    rows = [r for r in load_jsonl(os.path.join(config.DATA_DIR, "decision_journal.jsonl")) if not r.get("claimed")]
    events = load_jsonl(os.path.join(config.DATA_DIR, "journal_events.jsonl"))
    rows = rows[-last:] if last else rows
    for r in rows:
        r["trade_outcomes"] = outcomes((r.get("outcome") or {}).get("order_ids") or [], events)
    return rows


def view(rows):
    lines = []
    for r in rows:
        oc = r.get("outcome") or {}
        lines.append(f"{r.get('ts', '?')} {r.get('kind', 'plan'):8} {r.get('mode', '?'):8} plan={r.get('plan_id')} "
                     f"snap={r.get('snapshot_id')} valid={r.get('valid')} counts={oc.get('counts', {})}")
        if r.get("rationale"):
            lines.append("    why: " + str(r["rationale"])[:200])
        for a in r.get("actions") or []:
            lines.append(f"    [{a.get('index')}] {a.get('action')} {a.get('instId') or ''} -> {a.get('status')}"
                         + (f" ({'; '.join(a.get('reasons') or [])})" if a.get("reasons") else ""))
        for oid, o in (r.get("trade_outcomes") or {}).items():
            lines.append(f"    order {oid}: closed={o['closed']} pnl={o['realized_pnl']}")
        for e in r.get("errors") or []:
            lines.append("    ! " + e)
    return "\n".join(lines) if lines else "(no decisions yet)"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--last", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rows = recent(a.last)
    print(json.dumps(rows, sort_keys=True, ensure_ascii=False) if a.json else view(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
