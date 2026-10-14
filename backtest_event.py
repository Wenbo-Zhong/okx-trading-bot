"""Post-event breakout backtest (CPI / NFP / FOMC) on OKX 1m swap candles.
Rule: pre-event range = high/low of 1m bars in [t-R, t). From t+W, take the first 1m close outside the
range (within ENTRY_WIN min) with optional volume confirmation (bar vol >= VK x median pre-range 1m vol).
Enter at next bar open (+slippage). Stop: other side of range, or entry -/+ SK x ATR15 (pre-event).
Exit: 2R target + time stop, pure time stop, or chandelier trail; if stop and target hit in one bar -> stop.
Costs: 0.05% taker per side + 0.02% slippage per side. Risk 1% equity per trade, compounding.
"""
import json, glob, itertools, statistics as st, datetime as dt, sys
import numpy as np
FEE, SLIP, RISK = 0.0005, 0.0002, 0.01
ENTRY_WIN = 90
SPLIT = int(dt.datetime(2025, 4, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)

def load():
    ev = []
    for f in sorted(glob.glob("data/event_bt/*.json")):
        d = json.load(open(f)); d["inst"] = f.split("/")[-1].split("_")[0]
        if len(d["m1"]) < 600 or len(d["m5"]) < 100: continue
        d["a1"] = np.array(d["m1"]); d["a5"] = np.array(d["m5"]); ev.append(d)
    return ev

def atr15(a5):
    # resample 5m -> 15m, ATR(14)
    n = len(a5) // 3 * 3; a = a5[-n:]
    h = a[:, 2].reshape(-1, 3).max(1); l = a[:, 3].reshape(-1, 3).min(1); c = a[:, 4].reshape(-1, 3)[:, -1]
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    return tr[-14:].mean()

def sim(d, W, R, VK, stop, exitm, SK=1.5, N=8, TR=2.0):
    a = d["a1"]; t = d["t"]; ts = a[:, 0]
    pre = a[(ts >= t - R * 60_000) & (ts < t)]
    if len(pre) < R * 0.8: return None
    hi, lo = pre[:, 2].max(), pre[:, 3].min(); medv = np.median(pre[:, 5])
    A = atr15(d["a5"])
    idx = np.where((ts >= t + (W - 1) * 60_000) & (ts < t + (W + ENTRY_WIN) * 60_000))[0]  # bar closing at >= t+W
    for i in idx:
        c, v = a[i, 4], a[i, 5]
        side = 1 if c > hi else (-1 if c < lo else 0)
        if side == 0 or (VK and v < VK * medv): continue
        if i + 1 >= len(a): return None
        e = a[i + 1, 1] * (1 + side * SLIP)
        s = (lo if side == 1 else hi) if stop == "range" else e - side * SK * A
        risk = (e - s) * side
        if risk <= e * 0.0005: return None          # degenerate stop
        tp = e + side * 2 * risk if exitm == "2R" else None
        tend = a[i + 1, 0] + N * 3600_000
        best = e; trail = s
        for j in range(i + 1, len(a)):
            o, h, l, cl = a[j, 1:5]
            if exitm == "trail":
                stop_now = trail
            else:
                stop_now = s
            if (side == 1 and l <= stop_now) or (side == -1 and h >= stop_now):
                px = (min(o, stop_now) if side == 1 else max(o, stop_now)); why = "stop"; break
            if tp is not None and ((side == 1 and h >= tp) or (side == -1 and l <= tp)):
                px = tp; why = "tp"; break
            if a[j, 0] >= tend:
                px = cl; why = "time"; break
            if exitm == "trail":
                best = max(best, h) if side == 1 else min(best, l)
                trail = max(trail, best - TR * A) if side == 1 else min(trail, best + TR * A)
        else:
            px = a[-1, 4]; why = "eod"
        px *= (1 - side * SLIP)
        gross = (px - e) * side
        cost = FEE * (e + px)
        Rm = (gross - cost) / risk
        return dict(t=t, inst=d["inst"], kind=d["kind"], side=side, R=Rm, why=why, risk_pct=risk / e * 100)
    return None

def stats(tr):
    if not tr: return dict(n=0)
    R = np.array([x["R"] for x in sorted(tr, key=lambda x: x["t"])])
    eq = np.cumprod(1 + RISK * R); peak = np.maximum.accumulate(np.r_[1, eq])[1:]
    dd = (eq / peak - 1).min()
    return dict(n=len(R), win=(R > 0).mean() * 100, avgR=R.mean(), totR=R.sum(), ret=(eq[-1] - 1) * 100, mdd=dd * 100,
                t=R.mean() / (R.std(ddof=1) / len(R) ** .5) if len(R) > 2 else 0)

def fmt(s):
    if not s["n"]: return "n=0"
    return f"n={s['n']:3d} win={s['win']:4.0f}% avgR={s['avgR']:+.2f} totR={s['totR']:+6.1f} ret@1%={s['ret']:+6.1f}% mdd={s['mdd']:5.1f}% t={s['t']:+.2f}"

if __name__ == "__main__":
    ev = load(); print("event-symbol samples:", len(ev))
    grid = list(itertools.product([15, 30], [60, 120], [0, 2], ["range", "atr"], ["2R", "time", "trail"]))
    rows = []
    for W, R, VK, stop, ex in grid:
        tr = [x for d in ev if (x := sim(d, W, R, VK, stop, ex))]
        s = stats(tr); ins = stats([x for x in tr if x["t"] < SPLIT]); oos = stats([x for x in tr if x["t"] >= SPLIT])
        rows.append(((W, R, VK, stop, ex), s, ins, oos, tr))
    rows.sort(key=lambda r: -r[1].get("avgR", -9))
    print("\nALL VARIANTS (sorted by avgR, full sample) | IS = 2023-06..2025-03 | OOS = 2025-04..2026-10")
    for k, s, i, o, _ in rows:
        print(f"W={k[0]:2d} R={k[1]:3d} VK={k[2]} stop={k[3]:5s} exit={k[4]:5s} | {fmt(s)} | IS avgR={i.get('avgR',0):+.2f} n={i['n']} | OOS avgR={o.get('avgR',0):+.2f} n={o['n']}")
    avg = [r[1]["avgR"] for r in rows if r[1]["n"]]
    print(f"\ngrid: {len(avg)} variants, median avgR={np.median(avg):+.3f}, positive={sum(a>0 for a in avg)}/{len(avg)}; "
          f"OOS positive={sum(r[3].get('avgR',-1)>0 for r in rows)}/{len(rows)}; IS&OOS both positive={sum(r[2].get('avgR',-1)>0 and r[3].get('avgR',-1)>0 for r in rows)}")
    json.dump([[list(r[0]), r[1], r[2], r[3]] for r in rows], open("data/event_bt_results.json", "w"), default=float)
    for k, s, i, o, tr in rows[:3]:
        print(f"\n--- breakdown {k}")
        for kind in ["CPI", "NFP", "FOMC"]: print(f"  {kind:4s} {fmt(stats([x for x in tr if x['kind']==kind]))}")
        for inst in ["BTC", "ETH", "SOL"]: print(f"  {inst:4s} {fmt(stats([x for x in tr if x['inst'].startswith(inst)]))}")
        for sd in [1, -1]: print(f"  {'long ' if sd==1 else 'short'} {fmt(stats([x for x in tr if x['side']==sd]))}")
        print("  exits", {w: sum(1 for x in tr if x['why']==w) for w in ['tp','stop','time','trail','eod']}, " median stop dist %.2f%%" % np.median([x['risk_pct'] for x in tr]))
