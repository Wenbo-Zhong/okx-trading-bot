"""Coalescing wake dispatcher. Log-only unless enabled AND env URL set.

Every wake type (signals, news, position checks, mm_glitch, faults) shares ONE persisted budget
(2026-10-19, Codex review #5; mm_glitch no longer bypasses anything):
  * weekly cap  wake_max_per_week (ISO week, Asia/Shanghai). Ordinary wakes may use only
    wake_max_per_week - wake_fault_reserve_week; the reserve is kept for fault/halt wakes.
  * daily cap   wake_max_per_day for ordinary wakes; fault/halt wakes may go wake_fault_reserve_day above it.
  * spacing     ordinary: wake_min_interval_s; fault/halt: fault_cooldown_s.
  * wake_max_per_day <= 0 or wake_max_per_week <= 0 is a kill switch: nothing is dispatched.
  * per-kind re-arm cooldown (kind_cooldown_s) applies to every non-fault event: the same kind+inst+bar
    only wakes again if the hypothesis (direction), range (band) or risk materially changed.
Pending events older than pending_max_age_s are dropped (logged) rather than delivered stale."""
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
        self.blocked = None
        self.st = {"last_wake": 0, "day": "", "count": 0, "last_fault": 0, "kind_last": {},
                   "week": "", "week_count": 0, "week_faults": 0}
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

    @staticmethod
    def is_fault(event):
        return bool(event.get("severe")) or event.get("category") == "fault"

    def _material(self, event, last):
        """True if the event changes the hypothesis (direction flip with volume), range (band moved by
        >= material_band_frac of its width) or risk (risk_level changed) versus the last wake of its kind."""
        direction = self._direction(event)
        d = event.get("detail") or {}
        rel = float(d.get("rel_vol") or 0)
        if (direction is not None and last.get("direction") is not None and direction != last["direction"]
                and rel >= self.cfg.get("kind_cooldown_flip_rel_vol", 1.5)):
            return "direction_flip"
        band, lb = d.get("band"), last.get("band")
        if band and lb and len(band) == 2 and len(lb) == 2:
            w = max(abs(lb[1] - lb[0]), 1e-12)
            if max(abs(band[0] - lb[0]), abs(band[1] - lb[1])) >= self.cfg.get("material_band_frac", 0.25) * w:
                return "band_change"
        if d.get("risk_level") is not None and d.get("risk_level") != last.get("risk_level"):
            return "risk_change"
        return None

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
            # per-instrument/per-kind re-arm cooldown: applies to every non-fault event (incl. mm_glitch)
            ck = self._kind_key(event)
            kind_last = self.st.setdefault("kind_last", {})
            last = kind_last.get(ck)
            now = self.clock()
            cooldown = self.cfg.get("kind_cooldown_s", 7200)
            if last and not self.is_fault(event) and now - last.get("ts", 0) < cooldown:
                why = self._material(event, last)
                if not why:
                    self._log({"type": "suppressed", "reason": "kind_cooldown", "kind_key": ck,
                               "age_s": round(now - last.get("ts", 0), 1),
                               "direction": self._direction(event), "last_direction": last.get("direction"),
                               "rel_vol": float((event.get("detail") or {}).get("rel_vol") or 0)})
                    return False
                event = dict(event, material_change=why)
            event = dict(event, _queued=now)
            self.pending.append(event)
            if self.first_ts is None:
                self.first_ts = now
        return True

    def _roll(self, now):
        t = dt.datetime.fromtimestamp(now, SH)
        day, week = t.strftime("%Y-%m-%d"), "%d-W%02d" % t.isocalendar()[:2]
        if self.st.get("day") != day:
            self.st.update(day=day, count=0)
        if self.st.get("week") != week:
            self.st.update(week=week, week_count=0, week_faults=0)

    def budget_block(self, fault, now):
        """Reason the next wake is blocked (None = allowed). Shared by all wake types."""
        c = self.cfg
        per_day, per_week = c.get("wake_max_per_day", 8), c.get("wake_max_per_week", 30)
        if per_day <= 0 or per_week <= 0:
            return "wake budget disabled (limit 0)"
        reserve_w = min(c.get("wake_fault_reserve_week", 6), per_week)
        if fault:
            if now - self.st.get("last_fault", 0) < c["fault_cooldown_s"]:
                return "fault_cooldown"
            if self.st["count"] >= per_day + c.get("wake_fault_reserve_day", 2):
                return "daily cap (incl. fault reserve)"
            if self.st["week_count"] >= per_week:
                return "weekly cap"
            return None
        if now - self.st.get("last_wake", 0) < c["wake_min_interval_s"]:
            return "min_interval"
        if self.st["count"] >= per_day:
            return "daily cap"
        if self.st["week_count"] >= per_week - reserve_w:
            return "weekly cap (rest reserved for faults)"
        return None

    def tick(self):
        """Call periodically. Flushes pending events after the coalesce window if the shared budget allows."""
        with self.lock:
            if not self.pending:
                return None
            now = self.clock()
            max_age = self.cfg.get("pending_max_age_s", 7200)
            stale = [e for e in self.pending if now - e.get("_queued", now) > max_age]
            if stale:
                self.pending = [e for e in self.pending if e not in stale]
                self._log({"type": "dropped_stale", "count": len(stale),
                           "summary": "; ".join(f"{e['kind']} {e.get('inst', '')}" for e in stale)[:500]})
                if not self.pending:
                    self.first_ts = None
                    return None
                self.first_ts = min(e.get("_queued", now) for e in self.pending)
            severe = any(e.get("severe") for e in self.pending)
            fault = any(self.is_fault(e) for e in self.pending)
            regime = any(e.get("signal") == "mm_glitch" for e in self.pending)
            if not fault and now - self.first_ts < self.cfg["coalesce_s"]:
                return None
            self._roll(now)
            why = self.budget_block(fault, now)
            if why:
                if why != self.blocked:
                    self.blocked = why
                    self._log({"type": "suppressed", "reason": "budget: " + why, "pending": len(self.pending),
                               "day_count": self.st["count"], "week_count": self.st["week_count"]})
                return None
            self.blocked = None
            batch, self.pending, self.first_ts = self.pending, [], None
            self.st["last_wake"] = now
            self.st["count"] += 1
            self.st["week_count"] += 1
            if fault:
                self.st["last_fault"] = now
                self.st["week_faults"] = self.st.get("week_faults", 0) + 1
            if regime:
                self.st["regime_wakes"] = self.st.get("regime_wakes", 0) + 1
            kind_last = self.st.setdefault("kind_last", {})
            for e in batch:
                e.pop("_queued", None)
                d = e.get("detail") or {}
                kind_last[self._kind_key(e)] = {"ts": now, "direction": self._direction(e),
                                                "band": d.get("band"), "risk_level": d.get("risk_level")}
            # prune stale kind_last entries (keep last ~200)
            if len(kind_last) > 200:
                for k, _ in sorted(kind_last.items(), key=lambda kv: kv[1].get("ts", 0))[:-200]:
                    del kind_last[k]
            self._save()
        payload = {"source": "okxbot.monitor", "severe": severe, "fault": fault, "regime": regime, "count": len(batch),
                   "budget": {"day": self.st["count"], "week": self.st["week_count"],
                              "week_cap": self.cfg.get("wake_max_per_week", 30)},
                   "summary": "; ".join(f"{e['kind']} {e.get('inst', '')} {e.get('bar', '')}".strip() for e in batch)[:1500],
                   "events": batch}
        sent = False
        if self.enabled:
            try:
                sent = self.post(payload)
            except Exception as e:
                self._log({"type": "wake_error", "error": str(e)[:300]})
        self._log({"type": "wake", "mode": "webhook" if self.enabled else "log-only", "sent": sent,
                   "summary": payload["summary"], "severe": severe, "fault": fault, "regime": regime,
                   "day_count": self.st["count"], "week_count": self.st["week_count"]})
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
