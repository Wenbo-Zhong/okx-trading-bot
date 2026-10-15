"""Market-maker malfunction / high-frequency oscillation detector (1m bars + index price).

OBSERVATION ONLY: events carry signal='mm_glitch', observation_only=True, no_auto_execute=True.
Nothing here places or proposes orders; the planner decides manually and plans must pass
okxbot.plan's mm_glitch guardrails.

Rules (per 1m closed bar, baselines = median of the previous `mm_baseline_bars` bars):
  trigger : rel_vol >= 20, range >= max(100bp, 8x median range), and (ER5 <= 0.25 or range/body >= 3)
  strong  : rel_vol >= 10, range >= 80bp, ER5 <= 0.25
  confirm : >= 3 strong bars in the last 5 AND (basis-adjusted high/low deviation vs index >= 30bp
            OR >= 2 sign flips of the basis-adjusted close-vs-index gap with |gap| >= 15bp in the last 5)
  end     : 5 consecutive normal bars (rel_vol < 5 and range < 3x the baseline frozen at trigger)
Regime: wake every >= mm_wake_gap_s while active; caps 15 wakes / 30 min per regime, 2 regimes/day;
a cap hit emits a 'flatten' wake and closes the regime."""
import datetime as dt, json, os
from zoneinfo import ZoneInfo
from okxbot import config

SH = ZoneInfo(config.TIMEZONE)
STATE_F = os.path.join(config.DATA_DIR, "mm_glitch_state.json")
BP = 1e4

DEFAULTS = {
    "mm_insts": ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"],
    "mm_baseline_bars": 60,
    "mm_min_baseline_bars": 30,
    "mm_trig_rel_vol": 20.0, "mm_trig_range_bp": 100.0, "mm_trig_range_mult": 8.0,
    "mm_trig_er5": 0.25, "mm_trig_range_body": 3.0,
    "mm_strong_rel_vol": 10.0, "mm_strong_range_bp": 80.0, "mm_strong_er5": 0.25,
    "mm_confirm_strong": 3, "mm_confirm_window": 5,
    "mm_index_dev_bp": 30.0, "mm_cross_gap_bp": 15.0, "mm_cross_flips": 2,
    "mm_end_rel_vol": 5.0, "mm_end_range_mult": 3.0, "mm_end_bars": 5,
    "mm_wake_gap_s": 90, "mm_max_wakes_per_regime": 15, "mm_max_regime_s": 1800, "mm_max_regimes_per_day": 2,
    "mm_trigger_window_bars": 10,
    "mm_regime_cooldown_s": 1800,          # no new trigger/regime on an instrument this long after one ends/caps          # a trigger arms confirmation for this many bars
    "mm_min_depth_usdt": 1_000_000,        # book depth within mm_depth_band_pct of mid, each side
    "mm_depth_band_pct": 0.3,
}


def _med(v):
    s = sorted(v)
    return s[len(s) // 2] if s else 0.0


def _tag(ev):
    ev.update(signal="mm_glitch", observation_only=True, no_auto_execute=True, category="mm_glitch")
    return ev


class MMGlitch:
    def __init__(self, cfg=None, depth_of=None, state_path=STATE_F):
        self.cfg = dict(DEFAULTS, **{k: v for k, v in (cfg or {}).items() if k.startswith("mm_")})
        self.depth_of, self.state_path = depth_of, state_path
        self.bars, self.idx = {}, {}          # inst -> list of bars; index_inst -> {ts: bar}
        self.rows = {}                        # inst -> list of per-bar metric rows
        self.armed = {}                       # inst -> bars left in trigger window
        self.regime = {}                      # inst -> active regime dict
        self.day = {"day": "", "count": 0}
        self.cool = {}                        # inst -> epoch s until which triggers are suppressed

    @staticmethod
    def index_of(inst):
        return "-".join(inst.split("-")[:2])

    # ---------- data in ----------
    def add_index(self, index_inst, bar):
        d = self.idx.setdefault(index_inst, {})
        d[bar["ts"]] = bar
        if len(d) > 400:
            for k in sorted(d)[:-300]:
                del d[k]

    def seed(self, inst, bars):
        """Load history silently (no events)."""
        s = self.bars.setdefault(inst, [])
        have = {b["ts"] for b in s}
        s.extend(b for b in bars if b["ts"] not in have)
        s.sort(key=lambda b: b["ts"])
        del s[:-300]
        self.rows[inst] = []
        for i in range(len(s)):
            r = self._metrics(inst, s[:i + 1])
            if r:
                self.rows[inst].append(r)

    def _metrics(self, inst, s):
        cfg = self.cfg
        if len(s) < cfg["mm_min_baseline_bars"] + 1:
            return None
        b, prev = s[-1], s[-1 - cfg["mm_baseline_bars"]:-1]
        ib = self.idx.get(self.index_of(inst), {})
        rng = lambda x: (x["h"] - x["l"]) / x["c"] * BP if x["c"] else 0
        med_v = _med([x["v"] for x in prev]) or 1e-12
        med_r = _med([rng(x) for x in prev]) or 1e-9
        w = s[-5:]
        path = sum(x["h"] - x["l"] for x in w)
        er5 = abs(w[-1]["c"] - w[0]["o"]) / path if path else 1.0
        body = abs(b["c"] - b["o"])
        basis = [(x["c"] / ib[x["ts"]]["c"] - 1) for x in prev if x["ts"] in ib and ib[x["ts"]]["c"]]
        basis = _med(basis) if basis else 0.0
        dev = gap = None
        i = ib.get(b["ts"])
        if i and i["c"]:
            dev = max(abs(b["h"] / i["h"] - 1 - basis), abs(b["l"] / i["l"] - 1 - basis)) * BP
            gap = (b["c"] / i["c"] - 1 - basis) * BP
        return {"ts": b["ts"], "close": b["c"], "rel_vol": b["v"] / med_v, "range_bp": rng(b),
                "med_range_bp": med_r, "med_vol": med_v, "er5": er5,
                "range_body": (b["h"] - b["l"]) / body if body else 999.0,
                "index_dev_bp": dev, "gap_bp": gap, "basis_bp": basis * BP}

    # ---------- evaluation ----------
    def on_bar(self, inst, bar, now_s=None):
        """Feed one newly CLOSED 1m bar. Returns list of events."""
        cfg = self.cfg
        s = self.bars.setdefault(inst, [])
        if s and bar["ts"] <= s[-1]["ts"]:
            return []
        s.append(bar)
        del s[:-300]
        r = self._metrics(inst, s)
        if not r:
            return []
        rows = self.rows.setdefault(inst, [])
        rows.append(r)
        del rows[:-50]
        now_s = now_s if now_s is not None else bar["ts"] / 1000 + 60
        day = dt.datetime.fromtimestamp(now_s, SH).strftime("%Y-%m-%d")
        if self.day["day"] != day:
            self.day = {"day": day, "count": 0}
        reg = self.regime.get(inst)
        out = []
        if reg:
            return self._in_regime(inst, reg, r, now_s)
        trig = (r["rel_vol"] >= cfg["mm_trig_rel_vol"]
                and r["range_bp"] >= max(cfg["mm_trig_range_bp"], cfg["mm_trig_range_mult"] * r["med_range_bp"])
                and (r["er5"] <= cfg["mm_trig_er5"] or r["range_body"] >= cfg["mm_trig_range_body"]))
        if trig and self.cool.get(inst, 0) > now_s:
            out.append(self._ev("mm_glitch_trigger", inst, r, priority="low", extra={"reason": "post-regime cooldown"}))
            trig = False
        if trig and not self.armed.get(inst):
            out.append(self._ev("mm_glitch_trigger", inst, r, priority="normal"))
            self.armed[inst] = {"left": cfg["mm_trigger_window_bars"], "base_v": r["med_vol"],
                                "base_r": r["med_range_bp"], "trigger_ts": r["ts"]}
        a = self.armed.get(inst)
        if not a:
            return out
        a["left"] -= 1
        win = rows[-cfg["mm_confirm_window"]:]
        strong = sum(1 for x in win if x["rel_vol"] >= cfg["mm_strong_rel_vol"]
                     and x["range_bp"] >= cfg["mm_strong_range_bp"] and x["er5"] <= cfg["mm_strong_er5"])
        devs = [x["index_dev_bp"] for x in win if x["index_dev_bp"] is not None]
        gaps = [x["gap_bp"] for x in win if x["gap_bp"] is not None and abs(x["gap_bp"]) >= cfg["mm_cross_gap_bp"]]
        flips = sum(1 for p, q in zip(gaps, gaps[1:]) if (p > 0) != (q > 0))
        max_dev = max(devs) if devs else None
        idx_ok = (max_dev is not None and max_dev >= cfg["mm_index_dev_bp"]) or flips >= cfg["mm_cross_flips"]
        if strong >= cfg["mm_confirm_strong"] and idx_ok:
            del self.armed[inst]
            depth = None
            if self.depth_of:
                try:
                    depth = self.depth_of(inst)
                except Exception:
                    depth = None
            book_ok = depth is not None and depth >= cfg["mm_min_depth_usdt"]
            self.day["count"] += 1
            prices = [x["close"] for x in win]
            extra = {"strong_bars": strong, "max_index_dev_bp": max_dev, "gap_flips": flips,
                     "depth_usdt": depth, "book_ok": book_ok,
                     "band": [min(prices), max(prices)], "regimes_today": self.day["count"]}
            if self.day["count"] > cfg["mm_max_regimes_per_day"]:
                e = self._ev("mm_glitch_confirmed", inst, r, priority="low", extra=dict(extra, reason="daily regime cap reached; log only"))
                return out + [e]
            rid = f"{inst}:{r['ts']}"
            self.regime[inst] = {"id": rid, "start_s": now_s, "wakes": 1, "last_wake_s": now_s, "normal": 0,
                                 "base_v": a["base_v"], "base_r": a["base_r"], "lo": min(prices), "hi": max(prices),
                                 "book_ok": book_ok}
            e = self._ev("mm_glitch_confirmed", inst, r, priority="normal", extra=dict(extra, regime_id=rid))
            e["regime_wake"] = True
            out.append(e)
            self._save()
        elif a["left"] <= 0:
            del self.armed[inst]
        return out

    def _in_regime(self, inst, reg, r, now_s):
        cfg = self.cfg
        reg["lo"], reg["hi"] = min(reg["lo"], r["close"]), max(reg["hi"], r["close"])
        normal = r["rel_vol"] * r["med_vol"] < cfg["mm_end_rel_vol"] * reg["base_v"] and \
            r["range_bp"] < cfg["mm_end_range_mult"] * reg["base_r"]
        reg["normal"] = reg["normal"] + 1 if normal else 0
        x = {"regime_id": reg["id"], "age_s": int(now_s - reg["start_s"]), "wakes": reg["wakes"],
             "band": [reg["lo"], reg["hi"]], "rel_vol_vs_base": round(r["rel_vol"] * r["med_vol"] / reg["base_v"], 2),
             "normal_bars": reg["normal"]}
        if reg["normal"] >= cfg["mm_end_bars"]:
            del self.regime[inst]
            self.cool[inst] = now_s + cfg["mm_regime_cooldown_s"]
            self._save()
            e = self._ev("mm_glitch_end", inst, r, extra=x)
            e["regime_wake"] = True
            return [e]
        if now_s - reg["start_s"] >= cfg["mm_max_regime_s"] or reg["wakes"] >= cfg["mm_max_wakes_per_regime"]:
            del self.regime[inst]
            self.cool[inst] = now_s + cfg["mm_regime_cooldown_s"]
            self._save()
            e = self._ev("mm_glitch_cap_flatten", inst, r, priority="normal",
                         extra=dict(x, instruction="regime cap hit: FLATTEN any mm_glitch position now; no new mm_glitch entries for this regime"))
            e["regime_wake"] = True
            e["severe"] = True
            return [e]
        if reg["normal"] == 0 and now_s - reg["last_wake_s"] >= cfg["mm_wake_gap_s"]:
            reg["wakes"] += 1
            reg["last_wake_s"] = now_s
            x["wakes"] = reg["wakes"]
            self._save()
            e = self._ev("mm_glitch_update", inst, r, extra=x)
            e["regime_wake"] = True
            return [e]
        return [self._ev("mm_glitch_tick", inst, r, priority="low", extra=x)]

    def _ev(self, kind, inst, r, priority="normal", extra=None):
        d = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}
        if extra:
            d.update({k: (round(v, 4) if isinstance(v, float) else v) for k, v in extra.items()})
        return _tag({"kind": kind, "inst": inst, "bar": "1m", "bar_ts": r["ts"], "priority": priority, "detail": d})

    def active(self):
        return {i: dict(g) for i, g in self.regime.items()}

    def _save(self):
        if not self.state_path:
            return
        st = {"updated_s": max([g["last_wake_s"] for g in self.regime.values()] or [0]),
              "day": self.day, "regimes": self.active()}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, self.state_path)
