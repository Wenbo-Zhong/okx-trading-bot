"""Append-only trade-journal event log (data/journal_events.jsonl). Never raises: journaling must not
affect trading. Events: open, stop, close_request, close, alloc (see journal.py)."""
import json, os, time
from okxbot import config

EVENTS_F = os.path.join(config.DATA_DIR, "journal_events.jsonl")


def record(ev, **kw):
    try:
        row = {"ev": ev, "time_ms": kw.pop("time_ms", int(time.time() * 1000)), **kw}
        with open(EVENTS_F, "a") as f:
            f.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
    except Exception as e:  # pragma: no cover
        print(f"[journal] write failed: {e}")


def load():
    if not os.path.exists(EVENTS_F):
        return []
    out = []
    for line in open(EVENTS_F):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def market_of(inst_id):
    if not inst_id.endswith("-SWAP"):
        return "spot"
    return "tradfi swap" if inst_id.split("-")[0] in getattr(config, "TRADFI_LIST", []) else "crypto swap"
