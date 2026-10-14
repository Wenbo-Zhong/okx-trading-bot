import os, json, time, requests, datetime as dt
from event_dates import events
B = "https://www.okx.com"; D = "data/event_bt"
S = requests.Session()
def hist(inst, bar, end_ms, n, step_ms):
    rows = []; after = end_ms
    while len(rows) < n:
        for _ in range(5):
            try:
                j = S.get(B + "/api/v5/market/history-candles", params={"instId": inst, "bar": bar, "after": after, "limit": 100}, timeout=20).json()
                if j.get("code") == "0": break
            except Exception: pass
            time.sleep(1)
        d = j.get("data", [])
        if not d: break
        rows += d; after = int(d[-1][0]); time.sleep(0.12)
    rows = sorted({int(r[0]): [int(r[0])] + [float(x) for x in r[1:6]] for r in rows}.values())
    return rows
import sys
for inst in (sys.argv[1:] or ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]):
    for t, kind in events():
        f = f"{D}/{inst}_{t:%Y%m%d%H%M}.json"
        if os.path.exists(f): continue
        t0 = int(t.timestamp() * 1000)
        m1 = hist(inst, "1m", t0 + 12 * 3600_000, 15 * 60, 60_000)       # -3h .. +12h
        m5 = hist(inst, "5m", t0 - 3 * 3600_000, 21 * 12, 300_000)       # -24h .. -3h
        json.dump({"t": t0, "kind": kind, "m1": m1, "m5": m5}, open(f, "w"))
        print(inst, t, kind, len(m1), len(m5), flush=True)
