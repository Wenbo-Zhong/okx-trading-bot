"""US stock / commodity perpetuals on OKX (instCategory 3/4): eligibility check + session sizing.
Demo prices for several stock perps are broken (e.g. TSLA ~48% off), so every cycle we compare the demo
mark price against production and drop any instrument that drifts more than TRADFI_MAX_MARK_DEV."""
import datetime as dt
from zoneinfo import ZoneInfo
from okxbot import config

ET = ZoneInfo("America/New_York")


def base(inst_id):
    return inst_id.split("-")[0]


def is_tradfi(inst_id):
    return base(inst_id) in config.TRADFI_LIST


def session(now=None):
    """'cash' (Mon-Fri 09:30-16:00 ET), 'offhours' (weekday outside cash), 'weekend' (Sat/Sun ET).
    US exchange holidays are not modelled (treated as weekdays)."""
    t = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ET)
    if t.weekday() >= 5:
        return "weekend"
    m = t.hour * 60 + t.minute
    return "cash" if 9 * 60 + 30 <= m < 16 * 60 else "offhours"


def size_mult(inst_id, now=None):
    """Risk multiplier vs normal crypto sizing."""
    if not is_tradfi(inst_id):
        return 1.0
    m = config.TRADFI_RISK_MULT
    s = session(now)
    if s == "weekend" or (s == "offhours" and base(inst_id) not in config.TRADFI_COMMODITIES):
        m *= config.TRADFI_OFFSESSION_MULT
    return m


def eligible(x, prod_inst, prod_tick):
    """Return ([instIds ok], {instId: reason skipped}, {instId: info}). x = OKX client."""
    ok, skipped, info = [], {}, {}
    pm = {d["instId"]: float(d.get("markPx") or 0) for d in x.mark_prices("SWAP")}
    dm = {d["instId"]: float(d.get("markPx") or 0) for d in x.mark_prices("SWAP", demo=True)}
    demo_ids = {i["instId"] for i in x.instruments("SWAP", demo=True) if i.get("state") == "live"}
    for b in config.TRADFI_LIST:
        iid = f"{b}-USDT-SWAP"
        i, t = prod_inst.get(iid), prod_tick.get(iid)
        if not i or i.get("state") != "live" or i.get("instCategory") not in ("3", "4"):
            skipped[iid] = "not live on production"; continue
        if iid not in demo_ids:
            skipped[iid] = "not listed on demo"; continue
        last = float(t["last"] or 0) if t else 0
        vol = float(t.get("volCcy24h") or 0) * last if t else 0
        p, d = pm.get(iid, 0), dm.get(iid, 0)
        dev = abs(d / p - 1) if p and d else 1.0
        info[iid] = {"vol24h_usdt": round(vol), "mark_prod": p, "mark_demo": d, "dev_pct": round(dev * 100, 2)}
        if dev > config.TRADFI_MAX_MARK_DEV:
            skipped[iid] = f"demo mark off by {dev:.1%}"; continue
        if vol < config.TRADFI_MIN_VOL_USDT:
            skipped[iid] = f"24h volume {vol:,.0f} < {config.TRADFI_MIN_VOL_USDT:,.0f}"; continue
        ok.append(iid)
    return ok, skipped, info
