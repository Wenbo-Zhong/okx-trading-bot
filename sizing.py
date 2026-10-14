"""Pre-order affordability / limit clamps and the spot-vs-swap routing rule (pure functions, unit-tested).

Final size = min(risk-based size, affordable size, exchange cap from /account/max-size (already reflects
position-tier caps at the current leverage, existing same-direction position and pending orders),
instrument market-order cap), rounded down to lot size; skip if below the instrument minimum."""
import math

# Contract-spec fields that must come from the venue where orders EXECUTE (OKX demo specs differ from
# production for ~54 swaps, e.g. LIT ctVal 1 (prod) vs 10 (demo) -> 10x oversized orders).
SPEC_KEYS = ("ctVal", "ctMult", "ctValCcy", "lotSz", "minSz", "tickSz", "maxMktSz", "maxMktAmt", "maxLmtSz", "maxLmtAmt")


def merge_exec_specs(prod, demo):
    """prod/demo: {instId: instrument}. Returns prod records with execution-venue (demo) contract specs."""
    out = {}
    for k, i in prod.items():
        d = demo.get(k)
        out[k] = {**i, **{f: d[f] for f in SPEC_KEYS if d and f in d}, "_spec_src": "demo" if d else "prod"}
    return out


def order_sanity(sz, ct_val, ct_mult, px, stop, plan_notional, plan_risk, tol=1.2):
    """Hard check before a swap order. Returns (ok, info). Notional = sz*ctVal*ctMult*px; risk = |px-stop|*sz*ctVal*ctMult."""
    qty = float(sz) * float(ct_val) * float(ct_mult or 1)
    notional, risk = qty * px, abs(px - stop) * qty
    info = {"order_notional": round(notional, 2), "plan_notional": round(plan_notional, 2),
            "order_risk": round(risk, 2), "plan_risk": round(plan_risk, 2)}
    ok = notional <= plan_notional * tol + 1e-6 and risk <= plan_risk * tol + 1e-6
    return ok, info


def floor_step(x, step):
    step = float(step)
    n = math.floor(x / step + 1e-9)
    dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, dec)


def prefer_spot(side, lev, has_spot_pair, is_tradfi, max_lev_for_spot=1):
    """Long + effectively unlevered (lev <= max_lev_for_spot) + spot pair exists + not stock/commodity -> spot."""
    return side == 1 and lev <= max_lev_for_spot and has_spot_pair and not is_tradfi


def clamp_swap(contracts, lot, min_sz, lev, ct_val, px, avail_usdt, exch_max_ct=None, max_mkt_sz=None,
               buffer=0.95):
    """Returns (contracts, notes). contracts == 0 means skip."""
    notes = []
    caps = {"risk": contracts}
    if avail_usdt is not None:
        caps["margin"] = avail_usdt * buffer * lev / (ct_val * px)
    if exch_max_ct is not None:
        caps["exchange max-size/tier"] = exch_max_ct * buffer if exch_max_ct > 0 else 0.0
    if max_mkt_sz:
        caps["max market order"] = max_mkt_sz
    binding = min(caps, key=caps.get)
    out = floor_step(max(0.0, caps[binding]), lot)
    if binding != "risk":
        notes.append(f"clamped by {binding}: {contracts:g} -> {out:g} ct")
    if out < float(min_sz):
        notes.append(f"below min size {min_sz} after clamp -> skip")
        return 0.0, notes
    return out, notes


def clamp_spot(usdt, avail_usdt, px, exch_max_base=None, max_mkt_amt=None, min_sz=0.0, min_usdt=10.0,
               buffer=0.95):
    """Spot market buy sized in USDT. Returns (usdt, notes); 0 = skip."""
    notes = []
    caps = {"risk": usdt}
    if avail_usdt is not None:
        caps["available USDT"] = avail_usdt * buffer
    if exch_max_base is not None:
        caps["exchange max-size"] = exch_max_base * px * buffer
    if max_mkt_amt:
        caps["max market amount"] = max_mkt_amt * buffer
    binding = min(caps, key=caps.get)
    out = math.floor(max(0.0, caps[binding]) * 100) / 100
    if binding != "risk":
        notes.append(f"clamped by {binding}: {usdt:.2f} -> {out:.2f} USDT")
    if out < min_usdt or (px > 0 and out / px < float(min_sz or 0)):
        notes.append("below minimum order after clamp -> skip")
        return 0.0, notes
    return out, notes


def funding_harvest(rate_8h, last_settled=None, min_8h=0.0002):
    """True if longs clearly receive funding: current 8h-equivalent rate <= -min_8h and the last settled
    rate (if known) was not positive."""
    if rate_8h is None:
        return False
    return rate_8h <= -min_8h and (last_settled is None or last_settled <= 0)


def harvest_flip_action(rate_8h, count, flip_8h=0.0001, cycles=6, close_8h=0.0005):
    """Per-cycle check for a held funding-harvest long. Returns (new_count, action) where action is
    None, "flag" (persistently positive) or "close" (persistently positive AND at/above the entry-block level)."""
    if rate_8h is None:
        return count, None
    count = count + 1 if rate_8h >= flip_8h else 0
    if count >= cycles:
        return count, ("close" if rate_8h >= close_8h else "flag")
    return count, None


def depth_cap_usdt(asks, ref_px, max_slip=0.01, use_frac=0.8):
    """USDT that can be bought from the ask side without going more than max_slip above ref_px (mid/best ask),
    times use_frac. asks = [[px, sz, ...], ...] best first (OKX /market/books format)."""
    lim, tot = ref_px * (1 + max_slip), 0.0
    for a in asks:
        p, s = float(a[0]), float(a[1])
        if p > lim:
            break
        tot += p * s
    return tot * use_frac


def fill_summary(requested, acc_fill_sz, avg_px, quote=False, ct_val=1.0, tol=0.98):
    """Actual notional + whether the order is a partial fill (filled < tol x requested)."""
    notional = acc_fill_sz * avg_px * ct_val
    req = requested if quote else requested * avg_px * ct_val
    return round(notional, 4), bool(req > 0 and notional < req * tol)


# ---------- liquidity filter + stop-slippage buffer (demo order book) ----------
def walk_book(levels, qty):
    """Average fill price walking `levels` ([[px, sz_coins], ...] best first) for `qty` coins.
    Returns (avg_px, filled_qty); filled < qty means the visible book is exhausted."""
    rem, cost = qty, 0.0
    for lv in levels:
        p, s = float(lv[0]), float(lv[1])
        take = min(rem, s)
        cost += take * p; rem -= take
        if rem <= 1e-12:
            break
    filled = qty - rem
    return (cost / filled if filled > 0 else None), filled


def depth_within(levels, mid, pct):
    """Notional (quote) available within pct of mid on one side."""
    tot = 0.0
    for lv in levels:
        p, s = float(lv[0]), float(lv[1])
        if abs(p / mid - 1) > pct:
            break
        tot += p * s
    return tot


def liquidity_size(book, side, px, stop, notional, coins_per_unit=1.0, slip_floor=0.01, depth_pct=0.015,
                   min_cover=1.0, iters=4):
    """Shrink `notional` so that  qty * (|px-stop| + expected stop slippage) == planned risk, where the
    planned risk is notional*|px-stop|/px and expected slippage = max(slip_floor*stop, book impact of a
    market order of qty on the STOP side). Then require both entry-side and stop-side depth within
    depth_pct of mid >= min_cover * final notional. book = {"asks": [...], "bids": [...]} with sizes in
    contracts; coins_per_unit = ctVal*ctMult (1 for spot). Returns (new_notional, info, ok)."""
    asks = [[a[0], float(a[1]) * coins_per_unit] for a in book["asks"]]
    bids = [[b[0], float(b[1]) * coins_per_unit] for b in book["bids"]]
    mid = (float(asks[0][0]) + float(bids[0][0])) / 2
    entry_lv, stop_lv = (asks, bids) if side == 1 else (bids, asks)   # long: buy asks / stop sells bids
    dist = abs(px - stop)
    risk = notional * dist / px
    qty = notional / px
    slip = slip_floor * stop
    for _ in range(iters):
        avg, filled = walk_book(stop_lv, qty)
        best = float(stop_lv[0][0])
        impact = abs(avg - best) + abs(best - mid) if avg is not None and filled >= qty * 0.999 else float("inf")
        slip = max(slip_floor * stop, impact if impact != float("inf") else slip_floor * stop * 5)
        qty = min(notional / px, risk / (dist + slip))
    new_notional = qty * px
    d_entry, d_stop = depth_within(entry_lv, mid, depth_pct), depth_within(stop_lv, mid, depth_pct)
    ok = d_entry >= min_cover * new_notional and d_stop >= min_cover * new_notional
    info = {"planned_notional": round(notional, 2), "notional_after_slip": round(new_notional, 2),
            "est_stop_slip_pct": round(slip / stop * 100, 3), "planned_risk": round(risk, 2),
            "risk_incl_slip": round(qty * (dist + slip), 2), "depth_entry_usdt": round(d_entry, 2),
            "depth_stop_usdt": round(d_stop, 2), "depth_pct": depth_pct * 100}
    return new_notional, info, ok
