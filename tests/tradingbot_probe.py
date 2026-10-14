"""Probe OKX v5 tradingBot endpoints on the DEMO account (x-simulated-trading: 1).
Separate from the main bot: tradingBot paths are added to the whitelist only inside this process.
Usage: python3 tests/tradingbot_probe.py read      -> read-only/list + public param endpoints
       python3 tests/tradingbot_probe.py grid      -> create ONE tiny BTC-USDT spot grid, then stop it immediately"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from okx_client import OKX, OKXError
assert config.DEMO is True
READ = [
    ("GET", "/api/v5/tradingBot/grid/orders-algo-pending", {"algoOrdType": "grid"}),
    ("GET", "/api/v5/tradingBot/grid/orders-algo-pending", {"algoOrdType": "contract_grid"}),
    ("GET", "/api/v5/tradingBot/grid/orders-algo-pending", {"algoOrdType": "moon_grid"}),
    ("GET", "/api/v5/tradingBot/grid/orders-algo-history", {"algoOrdType": "grid"}),
    ("GET", "/api/v5/tradingBot/recurring/orders-algo-pending", {}),
    ("GET", "/api/v5/tradingBot/recurring/orders-algo-history", {}),
    ("GET", "/api/v5/tradingBot/signal/signals", {"signalSourceType": "1"}),
    ("GET", "/api/v5/tradingBot/signal/orders-algo-pending", {"algoOrdType": "contract"}),
    ("GET", "/api/v5/tradingBot/dca/ongoing-list", {"algoOrdType": "spot_dca"}),
    ("GET", "/api/v5/tradingBot/dca/history-list", {"algoOrdType": "spot_dca"}),
    ("GET", "/api/v5/tradingBot/dca/ongoing-list", {"algoOrdType": "contract_dca"}),
]
PUBLIC = [
    ("GET", "/api/v5/tradingBot/grid/ai-param", {"algoOrdType": "grid", "instId": "BTC-USDT"}),
    ("GET", "/api/v5/tradingBot/grid/ai-param", {"algoOrdType": "contract_grid", "instId": "BTC-USDT-SWAP", "direction": "neutral"}),
    ("GET", "/api/v5/tradingBot/public/rsi-back-testing", {"instId": "BTC-USDT", "timeframe": "15m", "thold": "30", "timePeriod": "14"}),
]
for m, p, _ in READ:
    config.PRIVATE_WHITELIST.add((m, p))
for p in ["/api/v5/tradingBot/grid/order-algo", "/api/v5/tradingBot/grid/stop-order-algo",
          "/api/v5/tradingBot/grid/min-investment"]:
    config.PRIVATE_WHITELIST.add(("POST", p))
config.PRIVATE_WHITELIST.add(("GET", "/api/v5/tradingBot/grid/orders-algo-details"))
x = OKX()
out = {}

def call(m, p, params=None, body=None, private=True, demo_public=False):
    k = f"{m} {p} {json.dumps(params or body or {}, sort_keys=True)}" + (" [demo-public]" if demo_public else "")
    try:
        d = x._req(m, p, params=params if m == "GET" else None, body=body, private=private, demo_public=demo_public)
        out[k] = {"ok": True, "n": len(d), "sample": d[:1]}
    except OKXError as e:
        out[k] = {"ok": False, "err": str(e)[:300]}
    print(k, "->", json.dumps(out[k])[:400]); time.sleep(0.3)
    return out[k]

mode = sys.argv[1] if len(sys.argv) > 1 else "read"
if mode == "read":
    for m, p, q in READ:
        call(m, p, q)
    for m, p, q in PUBLIC:
        call(m, p, q, private=False); call(m, p, q, private=False, demo_public=True)
elif mode == "grid":
    t = x._req("GET", "/api/v5/market/ticker", {"instId": "BTC-USDT"})[0]
    last = float(t["last"]); lo, hi = round(last * 0.97, 1), round(last * 1.03, 1)
    body = {"instId": "BTC-USDT", "algoOrdType": "grid", "maxPx": str(hi), "minPx": str(lo), "gridNum": "5",
            "runType": "1", "quoteSz": "300"}
    call("POST", "/api/v5/tradingBot/grid/min-investment", body={**{k: body[k] for k in ("instId", "algoOrdType", "maxPx", "minPx", "gridNum", "runType")}})
    r = call("POST", "/api/v5/tradingBot/grid/order-algo", body=body)
    if r["ok"]:
        algo_id = r["sample"][0]["algoId"]
        time.sleep(3)
        call("GET", "/api/v5/tradingBot/grid/orders-algo-details", {"algoOrdType": "grid", "algoId": algo_id})
        call("POST", "/api/v5/tradingBot/grid/stop-order-algo",
             body=[{"algoId": algo_id, "instId": "BTC-USDT", "algoOrdType": "grid", "stopType": "1"}])
        time.sleep(3)
        call("GET", "/api/v5/tradingBot/grid/orders-algo-pending", {"algoOrdType": "grid"})
        config.PRIVATE_WHITELIST.add(("GET", "/api/v5/tradingBot/grid/orders-algo-history"))
        call("GET", "/api/v5/tradingBot/grid/orders-algo-history", {"algoOrdType": "grid"})
json.dump(out, open(os.path.join(config.DATA_DIR, f"tradingbot_probe_{mode}.json"), "w"), indent=1)
