"""Coalescing wake dispatcher with hourly/daily caps. Log-only unless explicitly enabled AND env URL set."""
import datetime as dt, json, os, time, threading
import requests
from zoneinfo import ZoneInfo
from okxbot import config

LOG = os.path.join(config.DATA_DIR, "monitor_events.jsonl")
STATE = os.path.join(config.DATA_DIR, "monitor_wake_state.json")
SH = ZoneInfo(config.TIMEZONE)


class Waker:
    def __init__(self, cfg, enabled=False, clock=time.time, post=None, log_path=LOG, state_path=STATE):
        self.cfg, self.clock, self.log_path, self.state_path = cfg, clock, log_path, state_path
        self.url = os.environ.get("OKX_WAKE_WEBHOOK_URL", "")
        self.enabled = bool(enabled and self.url)
        self.post = post or self._post
        self.pending, self.first_ts, self.seen = [], None, {}
        self.lock = threading.Lock()
        self.st = {"last_wake": 0, "day": "", "count": 0, "last_fault": 0}
        try:
            with open(state_path) as f:
                self.st.update(json.load(f))
        except (FileNotFoundError, ValueError):
            pass

    def _log(self, rec):
        rec = dict(rec, logged_at=dt.datetime.now(dt.timezone.utc).isoformat())
        with open(self.log_path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def add(self, event):
        key = event.get("dedupe") or f"{event['kind']}:{event.get('inst')}:{event.get('bar')}:{event.get('bar_ts')}"
        with self.lock:
            if key in self.seen:
                return False
            self.seen[key] = self.clock()
            if len(self.seen) > 5000:
                for k in sorted(self.seen, key=self.seen.get)[:2500]:
                    del self.seen[k]
            self._log({"type": "event", **event})
            self.pending.append(event)
            if self.first_ts is None:
                self.first_ts = self.clock()
        return True

    def tick(self):
        """Call periodically. Flushes pending events after the coalesce window if caps allow."""
        with self.lock:
            if not self.pending:
                return None
            now = self.clock()
            severe = any(e.get("severe") for e in self.pending)
            if not severe and now - self.first_ts < self.cfg["coalesce_s"]:
                return None
            day = dt.datetime.fromtimestamp(now, SH).strftime("%Y-%m-%d")
            if self.st["day"] != day:
                self.st.update(day=day, count=0)
            if severe:
                if now - self.st["last_fault"] < self.cfg["fault_cooldown_s"]:
                    return None
            else:
                if now - self.st["last_wake"] < self.cfg["wake_min_interval_s"]:
                    return None
                if self.st["count"] >= self.cfg["wake_max_per_day"]:
                    return None
            batch, self.pending, self.first_ts = self.pending, [], None
            self.st["last_wake"] = now
            self.st["count"] += 1
            if severe:
                self.st["last_fault"] = now
            self._save()
        payload = {"source": "okxbot.monitor", "severe": severe, "count": len(batch),
                   "summary": "; ".join(f"{e['kind']} {e.get('inst', '')} {e.get('bar', '')}".strip() for e in batch)[:1500],
                   "events": batch}
        sent = False
        if self.enabled:
            try:
                sent = self.post(payload)
            except Exception as e:
                self._log({"type": "wake_error", "error": str(e)[:300]})
        self._log({"type": "wake", "mode": "webhook" if self.enabled else "log-only", "sent": sent,
                   "summary": payload["summary"], "severe": severe})
        return payload

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.st, f)
        os.replace(tmp, self.state_path)

    def _post(self, payload):
        headers = {"Content-Type": "application/json"}
        hdr = os.environ.get("OKX_WAKE_WEBHOOK_HEADER", "")
        key = os.environ.get("OKX_WAKE_WEBHOOK_KEY", "")
        if hdr and ":" in hdr:
            k, v = hdr.split(":", 1)
            headers[k.strip()] = v.strip()
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        text = json.dumps(payload, ensure_ascii=False)
        r = requests.post(self.url, data=text.encode(), headers=headers, timeout=15)
        return 200 <= r.status_code < 300
