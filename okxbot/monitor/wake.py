"""Coalescing wake dispatcher with hourly/daily caps and per-kind cooldown. Log-only unless enabled AND env URL set."""
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
        self.st = {"last_wake": 0, "day": "", "count": 0, "last_fault": 0, "kind_last": {}}
        try:
            with open(state_path) as f:
                self.st.update(json.load(f))
        except (FileNotFoundError, ValueError):
            pass

    def _log(self, rec):
        rec = dict(rec, logged_at=dt.datetime.now(dt.timezone.utc).isoformat())
        with open(self.log_path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    @staticmethod
    def _kind_key(event):
        return f"{event['kind']}:{event.get('inst') or ''}:{event.get('bar') or ''}"

    @staticmethod
    def _direction(event):
        if event.get("direction") is not None:
            return event["direction"]
        d = (event.get("detail") or {}).get("trend")
        return d

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
            # low-priority bar events are logged only (e.g. weak 15m trend_change)
            if event.get("priority") == "low" and not event.get("severe"):
                return False
            # per-instrument/per-kind re-arm cooldown
            ck = self._kind_key(event)
            kind_last = self.st.setdefault("kind_last", {})
            last = kind_last.get(ck)
            now = self.clock()
            cooldown = self.cfg.get("kind_cooldown_s", 7200)
            if last and not event.get("severe") and now - last.get("ts", 0) < cooldown:
                direction = self._direction(event)
                flipped = (direction is not None and last.get("direction") is not None
                           and direction != last["direction"])
                rel = float((event.get("detail") or {}).get("rel_vol") or 0)
                need = self.cfg.get("kind_cooldown_flip_rel_vol", 1.5)
                if not (flipped and rel >= need):
                    self._log({"type": "suppressed", "reason": "kind_cooldown", "kind_key": ck,
                               "age_s": round(now - last.get("ts", 0), 1),
                               "direction": direction, "last_direction": last.get("direction"),
                               "rel_vol": rel})
                    return False
            self.pending.append(event)
            if self.first_ts is None:
                self.first_ts = now
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
            kind_last = self.st.setdefault("kind_last", {})
            for e in batch:
                kind_last[self._kind_key(e)] = {"ts": now, "direction": self._direction(e)}
            # prune stale kind_last entries (keep last ~200)
            if len(kind_last) > 200:
                for k, _ in sorted(kind_last.items(), key=lambda kv: kv[1].get("ts", 0))[:-200]:
                    del kind_last[k]
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
