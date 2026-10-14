"""Minimal OKX v5 REST client. Public endpoints need no keys; private calls are
restricted to config.PRIVATE_WHITELIST (no withdrawal / transfer endpoints)."""
import base64, datetime as dt, hashlib, hmac, json, os, time
import requests
from okxbot import config


class OKXError(Exception):
    pass


class OKX:
    def __init__(self):
        self.key = os.environ.get("OKX_API_KEY", "")
        self.secret = os.environ.get("OKX_API_SECRET", "")
        self.passphrase = os.environ.get("OKX_API_PASSPHRASE", "")
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "okx-demo-bot/1.0"

    @property
    def has_keys(self):
        return bool(self.key and self.secret and self.passphrase)

    # ---------- transport ----------
    def _req(self, method, path, params=None, body=None, private=False, retries=3, demo_public=False):
        if private:
            p = path.split("?")[0]
            if (method, p) not in config.PRIVATE_WHITELIST or any(
                    w in p for w in ("withdraw", "transfer", "/asset/")):
                raise OKXError(f"Endpoint not allowed by bot policy: {method} {p}")
            if not self.has_keys:
                raise OKXError("No API keys configured")
        qs = ""
        if params:
            qs = "?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        req_path = path + qs
        data = json.dumps(body, separators=(",", ":")) if body is not None else ""
        last = None
        for attempt in range(retries):
            headers = {"Content-Type": "application/json"}
            # Demo header on private calls (and on public calls only when explicitly asked, e.g. to
            # list demo-tradable instruments). Market data for signals comes from production.
            if config.DEMO and (private or demo_public):
                headers["x-simulated-trading"] = "1"
            if private:
                ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                msg = ts + method + req_path + data
                sign = base64.b64encode(hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()
                headers.update({"OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": sign,
                                "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": self.passphrase})
            try:
                r = self.s.request(method, config.BASE_URL + req_path, data=data or None,
                                   headers=headers, timeout=15)
                j = r.json()
            except Exception as e:  # network / decode
                last = e
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429 or j.get("code") == "50011":
                time.sleep(2 * (attempt + 1)); last = j; continue
            if j.get("code") != "0":
                raise OKXError(f"{method} {path} -> code={j.get('code')} msg={j.get('msg')} data={j.get('data')}")
            return j["data"]
        raise OKXError(f"{method} {path} failed after retries: {last}")

    # ---------- public ----------
    def tickers(self, inst_type="SWAP"):
        return self._req("GET", "/api/v5/market/tickers", {"instType": inst_type})

    def instruments(self, inst_type="SWAP", demo=False):
        return self._req("GET", "/api/v5/public/instruments", {"instType": inst_type}, demo_public=demo)

    def mark_prices(self, inst_type="SWAP", demo=False):
        return self._req("GET", "/api/v5/public/mark-price", {"instType": inst_type}, demo_public=demo)

    def candles(self, inst_id, bar="1H", limit=300):
        """Most recent candles (oldest first). Columns: ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm"""
        rows = self._req("GET", "/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": limit})
        return list(reversed(rows))

    def recent_candles(self, inst_id, bar="1H", n=720):
        """Up to 1440 most recent candles via /market/candles pagination (oldest first)."""
        out, after = [], None
        while len(out) < n:
            rows = self._req("GET", "/api/v5/market/candles",
                             {"instId": inst_id, "bar": bar, "limit": 300, "after": after})
            if not rows:
                break
            out.extend(rows)
            after = rows[-1][0]
        out = {r[0]: r for r in out}
        return sorted(out.values(), key=lambda r: int(r[0]))[-n:]

    def history_candles(self, inst_id, bar="1H", n=4000):
        out, after = [], None
        while len(out) < n:
            rows = self._req("GET", "/api/v5/market/history-candles",
                             {"instId": inst_id, "bar": bar, "limit": 300, "after": after})
            if not rows:
                break
            out.extend(rows)
            after = rows[-1][0]
            time.sleep(0.12)
        out = {r[0]: r for r in out}
        return sorted(out.values(), key=lambda r: int(r[0]))[-n:]

    # ---------- private ----------
    def balance(self):
        return self._req("GET", "/api/v5/account/balance", private=True)

    def account_config(self):
        return self._req("GET", "/api/v5/account/config", private=True)

    def positions(self):
        return self._req("GET", "/api/v5/account/positions", {"instType": "SWAP"}, private=True)

    def positions_history(self, inst_id=None, limit=20):
        return self._req("GET", "/api/v5/account/positions-history",
                         {"instType": "SWAP", "instId": inst_id, "limit": limit}, private=True)

    def set_account_level(self, acct_lv):
        return self._req("POST", "/api/v5/account/set-account-level", body={"acctLv": str(acct_lv)}, private=True)

    def set_position_mode(self, pos_mode):
        return self._req("POST", "/api/v5/account/set-position-mode", body={"posMode": pos_mode}, private=True)

    def set_leverage(self, inst_id, lever, pos_side=None):
        body = {"instId": inst_id, "lever": str(lever), "mgnMode": config.TD_MODE}
        if pos_side:
            body["posSide"] = pos_side
        return self._req("POST", "/api/v5/account/set-leverage", body=body, private=True)

    def max_size(self, inst_id, td_mode, px=None):
        p = {"instId": inst_id, "tdMode": td_mode}
        if px:
            p["px"] = str(px)
        return self._req("GET", "/api/v5/account/max-size", p, private=True)

    def max_avail_size(self, inst_id, td_mode):
        return self._req("GET", "/api/v5/account/max-avail-size", {"instId": inst_id, "tdMode": td_mode}, private=True)

    def place_order(self, body):
        return self._req("POST", "/api/v5/trade/order", body=body, private=True)

    def cancel_order(self, inst_id, ord_id=None, cl_ord_id=None):
        body = {"instId": inst_id}
        if ord_id: body["ordId"] = ord_id
        if cl_ord_id: body["clOrdId"] = cl_ord_id
        return self._req("POST", "/api/v5/trade/cancel-order", body=body, private=True)

    def get_order(self, inst_id, ord_id):
        return self._req("GET", "/api/v5/trade/order", {"instId": inst_id, "ordId": ord_id}, private=True)

    def close_position(self, inst_id, pos_side=None):
        body = {"instId": inst_id, "mgnMode": config.TD_MODE, "autoCxl": True}
        if pos_side and pos_side != "net":
            body["posSide"] = pos_side
        return self._req("POST", "/api/v5/trade/close-position", body=body, private=True)

    def pending_algos(self, inst_id=None, inst_type=None):
        if inst_type is None:
            inst_type = "SPOT" if (inst_id and "-SWAP" not in inst_id) else "SWAP"
        return self._req("GET", "/api/v5/trade/orders-algo-pending",
                         {"ordType": "conditional,oco", "instType": inst_type, "instId": inst_id}, private=True)

    def place_algo(self, body):
        return self._req("POST", "/api/v5/trade/order-algo", body=body, private=True)

    def amend_algo(self, body):
        return self._req("POST", "/api/v5/trade/amend-algos", body=body, private=True)

    def cancel_algos(self, items):
        return self._req("POST", "/api/v5/trade/cancel-algos", body=items, private=True)
