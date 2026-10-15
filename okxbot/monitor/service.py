"""Long-running monitor: WS candles/tickers, REST backfill on (re)connect, news polling, state/fault checks."""
import collections, datetime as dt, json, logging, os, re, threading, time
import websocket
websocket.setdefaulttimeout(30)
from okxbot import config, news
from okxbot.okx_client import OKX
from okxbot.monitor import settings, signals, watchlist
from okxbot.monitor.mmglitch import MMGlitch
from okxbot.monitor.wake import Waker

log = logging.getLogger("okxbot.monitor")
STATE_F = os.path.join(config.DATA_DIR, "state.json")
ALERTS_F = os.path.join(config.DATA_DIR, "alerts.jsonl")
STATUS_F = os.path.join(config.DATA_DIR, "monitor_status.json")
SEVERE_ERR = re.compile(r"stop|protect|unprotected|algo|order|reconcile|close|circuit|halt", re.I)


def read_state():
    try:
        with open(STATE_F) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


class ConnGate:
    """Global limit on new WS connections per second (OKX: 3/s per IP)."""
    def __init__(self, per_s):
        self.gap, self.last, self.lock = 1.0 / per_s + 0.05, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            d = self.last + self.gap - time.time()
            if d > 0:
                time.sleep(d)
            self.last = time.time()


class Feed(threading.Thread):
    def __init__(self, name, url, args, cfg, gate, on_msg, on_connect):
        super().__init__(daemon=True, name=name)
        self.url, self.args, self.cfg, self.gate = url, args, cfg, gate
        self.on_msg, self.on_connect = on_msg, on_connect
        self.ops = collections.deque()
        self.stop_ev = threading.Event()
        self.ws, self.connects = None, 0

    def _sub(self, ws):
        n = self.cfg["ws_args_per_op"]
        for i in range(0, len(self.args), n):
            now = time.time()
            while self.ops and now - self.ops[0] > 3600:
                self.ops.popleft()
            if len(self.ops) >= self.cfg["ws_sub_ops_per_hour"] - 5:
                log.error("%s: subscribe-op budget exhausted; waiting", self.name)
                time.sleep(max(1, 3600 - (now - self.ops[0])))
            self.ops.append(time.time())
            ws.send(json.dumps({"op": "subscribe", "args": self.args[i:i + n]}))

    def run(self):
        backoff = 1
        while not self.stop_ev.is_set():
            self.gate.wait()
            opened = {}

            def on_open(ws):
                opened["t"] = time.time()
                self.connects += 1
                self._sub(ws)
                threading.Thread(target=self.on_connect, args=(self, self.connects > 1), daemon=True).start()

            def on_message(ws, m):
                if m == "pong":
                    return
                try:
                    self.on_msg(json.loads(m))
                except Exception:
                    log.exception("%s message handler", self.name)

            self.ws = websocket.WebSocketApp(self.url, on_open=on_open, on_message=on_message,
                                             on_error=lambda ws, e: log.warning("%s ws error: %s", self.name, e))
            try:
                self.ws.run_forever(ping_interval=20, ping_timeout=10, ping_payload="ping")
            except Exception as e:
                log.warning("%s run_forever: %s", self.name, e)
            if self.stop_ev.is_set():
                break
            backoff = 1 if opened and time.time() - opened["t"] > 60 else min(backoff * 2, 60)
            log.warning("%s disconnected; reconnect in %ss", self.name, backoff)
            self.stop_ev.wait(backoff)

    def stop(self):
        self.stop_ev.set()
        if self.ws:
            self.ws.close()


class Monitor:
    def __init__(self, enable_wakes=False, cfg=None):
        self.cfg = cfg or settings.load()
        self.client = OKX()
        self.waker = Waker(self.cfg, enabled=enable_wakes)
        self.latch = signals.Armed()
        self.bars = {}          # (inst, bar) -> list of closed bars
        self.done = {}          # (inst, bar) -> last evaluated closed ts (dedupe)
        self.last_px = {}
        self.breach = {}
        self.lock = threading.RLock()
        self.rest_lock = threading.Lock()
        self.news_seen = set()
        self.alerts_pos = os.path.getsize(ALERTS_F) if os.path.exists(ALERTS_F) else 0
        self.feeds = []
        self.started = time.time()
        self.mm = MMGlitch(self.cfg, depth_of=self.book_depth)

    # ----- watchlist -----
    def build_watchlist(self, force=False):
        self.wl = watchlist.build(self.client, read_state(), self.cfg, force=force)
        return self.wl

    def book_depth(self, inst):
        """Min(bid, ask) USDT depth within mm_depth_band_pct of mid (REST, called only on confirmation)."""
        with self.rest_lock:
            ob = self.client._req("GET", "/api/v5/market/books", {"instId": inst, "sz": 400})[0]
            spec = self.client._req("GET", "/api/v5/public/instruments", {"instType": "SWAP", "instId": inst})[0]
        ct = float(spec.get("ctVal") or 1)
        mid = (float(ob["bids"][0][0]) + float(ob["asks"][0][0])) / 2
        band = self.mm.cfg["mm_depth_band_pct"] / 100
        side = lambda rows: sum(float(p) * float(q) * ct for p, q, *_ in rows if abs(float(p) / mid - 1) <= band)
        return min(side(ob["bids"]), side(ob["asks"]))

    def mm_backfill(self):
        """Seed the mm_glitch detector with ~100 1m bars + index bars (silent)."""
        for inst in self.mm.cfg["mm_insts"]:
            ix = MMGlitch.index_of(inst)
            with self.rest_lock:
                try:
                    rows = self.client.candles(inst, bar="1m", limit=100)
                    time.sleep(self.cfg["rest_min_gap_s"])
                    irows = self.client._req("GET", "/api/v5/market/index-candles", {"instId": ix, "bar": "1m", "limit": 100})
                    time.sleep(self.cfg["rest_min_gap_s"])
                except Exception as e:
                    log.warning("mm backfill %s: %s", inst, e)
                    continue
            with self.lock:
                for r in irows:
                    if len(r) < 6 or r[5] == "1":
                        self.mm.add_index(ix, self._bar(r[:5] + ["0"]))
                self.mm.seed(inst, [self._bar(r) for r in reversed(rows) if len(r) < 9 or r[8] == "1"])
        log.info("mm_glitch backfill done")

    # ----- REST backfill -----
    def backfill(self, feed, reconnect):
        threading.Thread(target=self.mm_backfill, daemon=True).start()
        for arg in feed.args:
            if not arg["channel"].startswith("candle") or arg["channel"] == "candle1m":
                continue
            inst, bar = arg["instId"], arg["channel"][6:]
            with self.rest_lock:
                try:
                    rows = self.client.candles(inst, bar=bar, limit=self.cfg["history_bars"])
                except Exception as e:
                    log.warning("backfill %s %s: %s", inst, bar, e)
                    continue
                finally:
                    time.sleep(self.cfg["rest_min_gap_s"])
            closed = [self._bar(r) for r in reversed(rows) if len(r) < 9 or r[8] == "1"]
            with self.lock:
                k = (inst, bar)
                have = {b["ts"]: b for b in self.bars.get(k, [])}
                have.update({b["ts"]: b for b in closed})
                self.bars[k] = [have[t] for t in sorted(have)][-self.cfg["history_bars"]:]
                if k not in self.done:      # first load: seed, do not alert on history
                    self.done[k] = self.bars[k][-1]["ts"] if self.bars[k] else 0
                    for i in range(80, len(self.bars[k])):   # warm latches silently
                        signals.evaluate(inst, bar, self.bars[k][:i + 1], self.group(inst), self.cfg, self.latch)
                else:                       # reconnect: evaluate bars missed while down
                    self._evaluate_new(k)
        log.info("%s backfill done (reconnect=%s)", feed.name, reconnect)

    @staticmethod
    def _bar(r):
        return {"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}

    def group(self, inst):
        return self.wl["instruments"].get(inst, {}).get("group", "held")

    def _evaluate_new(self, k):
        inst, bar = k
        series = self.bars.get(k, [])
        for i, b in enumerate(series):
            if b["ts"] <= self.done.get(k, 0):
                continue
            self.done[k] = b["ts"]
            for e in signals.evaluate(inst, bar, series[:i + 1], self.group(inst), self.cfg, self.latch):
                e["bar_ts"] = b["ts"]
                e["bar_close_time"] = dt.datetime.fromtimestamp(b["ts"] / 1000, dt.timezone.utc).isoformat()
                self.waker.add(e)

    # ----- WS handlers -----
    def on_msg(self, m):
        arg, data = m.get("arg") or {}, m.get("data")
        if m.get("event") == "error":
            log.error("ws error event: %s", m)
            return
        if not data:
            return
        ch = arg.get("channel", "")
        if ch == "index-candle1m":
            with self.lock:
                for r in data:
                    if r[5] == "1":
                        self.mm.add_index(arg["instId"], self._bar(r[:5] + ["0"]))
            return
        if ch == "candle1m":
            with self.lock:
                for r in data:
                    if r[8] != "1":
                        continue
                    for e in self.mm.on_bar(arg["instId"], self._bar(r), time.time()):
                        e["bar_close_time"] = dt.datetime.fromtimestamp(e["bar_ts"] / 1000, dt.timezone.utc).isoformat()
                        e["dedupe"] = f"{e['kind']}:{e['inst']}:{e['bar_ts']}"
                        self.waker.add(e)
            return
        if ch == "tickers":
            for d in data:
                self.last_px[d["instId"]] = float(d.get("last") or 0)
        elif ch.startswith("candle"):
            k = (arg["instId"], ch[6:])
            with self.lock:
                for r in data:
                    if r[8] != "1":
                        continue
                    b = self._bar(r)
                    s = self.bars.setdefault(k, [])
                    if s and b["ts"] <= s[-1]["ts"]:
                        continue   # duplicate push
                    s.append(b)
                    del s[:-self.cfg["history_bars"]]
                if k in self.done:
                    self._evaluate_new(k)

    # ----- periodic checks -----
    def check_positions(self):
        st = read_state()
        if st.get("halted"):
            self.waker.add({"kind": "circuit_breaker_halted", "category": "fault", "severe": True,
                            "dedupe": f"halted:{st.get('day')}", "detail": {"halted": st.get("halted")}})
        for inst, pos in (st.get("positions") or {}).items():
            px = self.last_px.get(inst)
            with self.lock:
                s = self.bars.get((inst, "1H"), [])
                a = signals.atr(s[-60:], self.cfg["atr_n"]) if len(s) > 20 else None
            for e in signals.position_checks(inst, pos, px, a, self.cfg, self.latch, self.breach, time.time()):
                e["dedupe"] = f"{e['kind']}:{inst}:{int(time.time() // 60)}"
                if e["kind"] == "max_hold_exceeded":
                    e["detail"]["enforced"] = self.enforce_max_hold(inst, pos)
                self.waker.add(e)
        held = set(watchlist.held_instruments(st))
        missing = held - set(self.wl["instruments"])
        if missing:
            log.info("new held instruments %s -> resubscribe", missing)
            self.restart_feeds()

    def enforce_max_hold(self, inst, pos):
        """Close an mm_glitch/range position past max_hold_until (DEMO keys required). Returns outcome."""
        if not (config.DEMO and getattr(config, "MM_GLITCH_MONITOR_ENFORCE", False)):
            return "disabled"
        if pos.get("signal") != "mm_glitch":
            return "not mm_glitch"
        if not self.client.has_keys:
            return "no keys (log-only)"
        try:
            self.client.close_position(inst, None)
            algos = self.client.pending_algos(inst)
            if algos:
                self.client.cancel_algos([{"algoId": x["algoId"], "instId": inst} for x in algos])
            return "closed"
        except Exception as e:
            log.exception("max-hold close %s", inst)
            return f"error: {e}"[:200]

    def check_alert_faults(self):
        if not os.path.exists(ALERTS_F):
            return
        size = os.path.getsize(ALERTS_F)
        if size < self.alerts_pos:
            self.alerts_pos = 0
        with open(ALERTS_F) as f:
            f.seek(self.alerts_pos)
            lines = f.readlines()
            self.alerts_pos = f.tell()
        for ln in lines:
            try:
                a = json.loads(ln)
            except ValueError:
                continue
            if a.get("type") == "error" and SEVERE_ERR.search(a.get("msg", "")):
                self.waker.add({"kind": "protection_fault", "category": "fault", "severe": True,
                                "dedupe": "fault:" + a.get("key", a.get("msg", "")[:80]),
                                "detail": {"time": a.get("time"), "msg": a.get("msg", "")[:500]}})

    def poll_news(self):
        items, errs = news.fetch_items(max_age_h=self.cfg["news_max_age_h"])
        watched = {watchlist.base(i) for i in self.wl["instruments"]}
        hi = [it for it in items if news.is_high_impact(it["text"])]
        for it in hi:
            key = it.get("url") or it["title"]
            if key in self.news_seen:
                continue
            words = {w for w in re.findall(r"[a-z]{5,}", it["title"].lower())}
            confirm = {o["src"] for o in hi if o["src"] != it["src"]
                       and len(words & set(re.findall(r"[a-z]{5,}", o["title"].lower()))) >= 2}
            coins = sorted(b for b in watched if any(n in it["text"].lower()
                           for n in news.NAMES.get(b, [b.lower()]) if len(n) > 2))
            self.news_seen.add(key)
            self.waker.add({"kind": "material_news", "category": "news", "dedupe": "news:" + key,
                            "detail": {"title": it["title"], "source": it["src"], "url": it.get("url"),
                                       "event_time": it["published_at"], "coins": coins,
                                       "confirmation": ("confirmed by " + ", ".join(sorted(confirm))) if confirm
                                       else "single source, unconfirmed"}})
        if errs:
            log.info("news errors: %s", errs)

    # ----- lifecycle -----
    def feed_args(self):
        cand, tick = [], []
        for inst, e in self.wl["instruments"].items():
            for b in e["bars"]:
                cand.append({"channel": "candle" + b, "instId": inst})
            if e.get("held"):
                tick.append({"channel": "tickers", "instId": inst})
        for inst in self.mm.cfg["mm_insts"]:
            cand.append({"channel": "candle1m", "instId": inst})
            cand.append({"channel": "index-candle1m", "instId": MMGlitch.index_of(inst)})
        st = read_state()
        for inst in st.get("positions", {}):
            if {"channel": "tickers", "instId": inst} not in tick:
                tick.append({"channel": "tickers", "instId": inst})
        return cand, tick

    def start_feeds(self):
        gate = getattr(self, "gate", None) or ConnGate(self.cfg["ws_new_conn_per_s"])
        self.gate = gate
        cand, tick = self.feed_args()
        self.feeds = [Feed("business", self.cfg["ws_business"], cand, self.cfg, gate, self.on_msg, self.backfill)]
        if tick:
            self.feeds.append(Feed("public", self.cfg["ws_public"], tick, self.cfg, gate, self.on_msg, lambda f, r: None))
        for f in self.feeds:
            f.start()

    def restart_feeds(self):
        for f in self.feeds:
            f.stop()
        self.build_watchlist()
        self.start_feeds()

    def write_status(self):
        with self.lock:
            st = {"pid": os.getpid(), "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "wakes_enabled": self.waker.enabled, "rotation": self.wl["rotation"],
                  "instruments": len(self.wl["instruments"]), "series_loaded": len(self.done),
                  "feeds": {f.name: {"alive": f.is_alive(), "connects": f.connects, "args": len(f.args)} for f in self.feeds},
                  "pending_events": len(self.waker.pending), "mm_glitch_regimes": self.mm.active(), "wake_state": self.waker.st}
        tmp = STATUS_F + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=1)
        os.replace(tmp, STATUS_F)

    def run(self, stop_ev=None):
        stop_ev = stop_ev or threading.Event()
        self.build_watchlist()
        self.start_feeds()
        nxt = {"pos": 0, "news": time.time() + 60, "status": 0, "rot": time.time() + 3600}
        while not stop_ev.is_set():
            now = time.time()
            try:
                if now >= nxt["pos"]:
                    nxt["pos"] = now + self.cfg["state_poll_s"]
                    self.check_positions()
                    self.check_alert_faults()
                if now >= nxt["news"]:
                    nxt["news"] = now + self.cfg["news_poll_s"]
                    self.poll_news()
                if now >= nxt["rot"]:
                    nxt["rot"] = now + 3600
                    if watchlist.rotation_key() != self.wl["rotation"]:
                        self.restart_feeds()
                self.waker.tick()
                if now >= nxt["status"]:
                    nxt["status"] = now + 60
                    self.write_status()
            except Exception:
                log.exception("monitor loop")
            stop_ev.wait(2)
        for f in self.feeds:
            f.stop()
