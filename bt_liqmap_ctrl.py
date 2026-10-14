"""Control for bt_liqmap: widen stops WITHOUT cluster info, to see if the gain is just 'wider stops, same TP'."""
import json, random, pandas as pd, config, backtest
exec(open("bt_liqmap.py").read().split("rows = []")[0].split("stats = ")[0])
def widen_all(pct):
    def h(inst, ts, side, entry, stop, atr):
        new = stop - side * entry * pct / 100
        return new if abs(entry - new) / entry <= config.MAX_STOP_PCT else stop
    return h
def widen_random(pct, p=0.51, seed=0):
    rnd = random.Random(seed)
    def h(inst, ts, side, entry, stop, atr):
        if rnd.random() > p: return stop
        new = stop - side * entry * pct / 100
        return new if abs(entry - new) / entry <= config.MAX_STOP_PCT else stop
    return h
for name, hk in [("widen ALL stops +0.89%", widen_all(0.89)), ("widen random 51% +0.89% seed0", widen_random(0.89, seed=0)),
                 ("widen random 51% +0.89% seed1", widen_random(0.89, seed=1)), ("widen random 51% +0.89% seed2", widen_random(0.89, seed=2))]:
    r, tr, cv = backtest.run(U30, 8760, start_eq=10000, t_from=t_from, stop_hook=hk)
    print(name, {k: r[k] for k in ["return_pct", "max_dd_pct", "trades", "win_rate_pct", "profit_factor"]}, flush=True)
