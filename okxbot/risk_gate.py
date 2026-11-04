"""Independent risk veto for planner opens — cannot be skipped by --execute.

Returns APPROVE / VETO with reasons. Covers: kill pauses, stale market data,
no-chase (price vs plan entry), and correlation-group risk budgets.
"""
import time
from okxbot import config
from okxbot import risk as R


def base_ccy(inst_id):
    return str(inst_id or "").split("-")[0]


def correlation_group(inst_id):
    """Map instrument to a correlation bucket id."""
    ccy = base_ccy(inst_id)
    for name, members in (getattr(config, "CORRELATION_GROUPS", None) or {}).items():
        if ccy in members:
            return name
    return "solo:" + ccy


def _group_side_risk(state, snapshot, group, side, unit_budget=None):
    """Sum planned_risk_usdt of open positions in group with same side (1 long / -1 short).

    Positions without a recorded planned_risk_usdt (and without a usable stop) count as one
    full unit_budget so a same-direction sibling cannot sneak past the correlation cap.
    """
    total = 0.0
    account = (snapshot or {}).get("account") or {}
    positions = account.get("positions") or []
    state_pos = dict(state.get("positions") or {})
    state_pos.update((state.get("spot") or {}).get("longs") or {})
    seen = set()

    def _side_of(p, rec):
        pside = p.get("side") if p else None
        if pside in ("long", "buy"):
            return 1
        if pside in ("short", "sell"):
            return -1
        # net-mode swap: negative pos => short
        try:
            raw = float(p.get("pos") or p.get("size") or 0) if p else 0
            if raw < 0:
                return -1
            if raw > 0 and int(rec.get("side") or 0) == 0:
                return 1
        except (TypeError, ValueError):
            pass
        return int(rec.get("side") or 0)

    def _risk(rec, p=None):
        if rec.get("planned_risk_usdt") is not None:
            try:
                return float(rec["planned_risk_usdt"])
            except (TypeError, ValueError):
                pass
        try:
            entry = float(rec.get("entry") or (p or {}).get("avg_px") or 0)
            stop = float(rec.get("stop") or 0)
            qty = abs(float(rec.get("qty") or rec.get("contracts") or (p or {}).get("size")
                            or (p or {}).get("pos") or 0))
            ct = float((rec.get("spec") or {}).get("ctVal") or 1)
            if entry > 0 and stop > 0 and qty > 0:
                return abs(entry - stop) * qty * ct
        except (TypeError, ValueError):
            pass
        # Unknown risk but position exists: consume a full correlation unit.
        return float(unit_budget or 0)

    for p in positions:
        inst = p.get("instId")
        if not inst or correlation_group(inst) != group:
            continue
        rec = state_pos.get(inst) or {}
        s = _side_of(p, rec)
        if s != side:
            continue
        seen.add(inst)
        total += _risk(rec, p)
    for inst, rec in state_pos.items():
        if inst in seen or correlation_group(inst) != group:
            continue
        if int(rec.get("side") or 0) != side:
            continue
        total += _risk(rec)
    return total


def _planned_entry(action):
    for key in ("entry", "limit_px"):
        if action.get(key) is not None:
            try:
                return float(action[key])
            except (TypeError, ValueError):
                pass
    return None


def _instrument_staleness_s(data, now_s=None):
    """Age in seconds of the newest closed candle / last update; None if unknown."""
    now_s = now_s if now_s is not None else time.time()
    newest_ms = None
    candles = (data or {}).get("candles") or {}
    for tf, block in candles.items():
        bars = (block or {}).get("bars") or []
        if not bars:
            continue
        try:
            ts = int(bars[-1][0])
        except (TypeError, ValueError, IndexError):
            continue
        newest_ms = ts if newest_ms is None else max(newest_ms, ts)
    # Prefer explicit data timestamps if present
    for key in ("last_ts_ms", "ticker_ts_ms"):
        try:
            ts = int((data or {}).get(key))
            newest_ms = ts if newest_ms is None else max(newest_ms, ts)
        except (TypeError, ValueError):
            pass
    if newest_ms is None:
        return None
    # OKX candle ts is bar open; treat bar as "fresh" for its timeframe by adding nothing —
    # stale check uses age of last closed bar open vs now (conservative).
    return max(0.0, now_s - newest_ms / 1000.0)


def _open_risk_usdt(action, snapshot):
    """Best-effort planned risk for a new open from size.risk_usdt or stop distance."""
    size = action.get("size") or {}
    if isinstance(size, dict) and "risk_usdt" in size:
        try:
            return float(size["risk_usdt"])
        except (TypeError, ValueError):
            pass
    inst = action.get("instId")
    data = ((snapshot or {}).get("instruments") or {}).get(inst) or {}
    try:
        px = float(data.get("last") or 0)
        stop = float(action.get("stop") or 0)
        side = 1 if action.get("side") == "long" else -1
        entry = _planned_entry(action) or px
        dist = (entry - stop) * side
        if dist <= 0:
            return 0.0
        if "notional_usdt" in size:
            return float(size["notional_usdt"]) * (dist / entry)
        if "contracts" in size:
            units = float((data.get("spec") or {}).get("ctVal") or 1)
            return float(size["contracts"]) * units * dist
    except (TypeError, ValueError):
        pass
    return 0.0


def check_open(action, snapshot, state, now=None, reserved_groups=None, planned_risk_usdt=None):
    """Gate one open action. reserved_groups: {(group, side): risk_usdt} already reserved in this plan.
    planned_risk_usdt: actual sized risk after clamps (preferred over raw action size).

    Returns dict: decision ('APPROVE'|'VETO'), reasons [str], details {}.
    """
    reasons = []
    details = {}
    now_s = time.time() if now is None else (now.timestamp() if hasattr(now, "timestamp") else float(now))
    R.clear_expired_pauses(state, now_s)

    blocks = R.active_entry_blocks(state, now_s)
    if blocks:
        reasons.extend(blocks)

    inst = action.get("instId")
    data = ((snapshot or {}).get("instruments") or {}).get(inst) or {}
    # Stale market data
    age = _instrument_staleness_s(data, now_s)
    stale_lim = float(getattr(config, "KILL_STALE_DATA_S", 900))
    details["data_age_s"] = age
    if age is not None and age > stale_lim:
        reasons.append(f"stale data: last candle/ticker age {age:.0f}s > {stale_lim:.0f}s")
    elif age is None and (data.get("candles") or data.get("last") is None):
        # If instruments block has no usable timestamp and no last, treat as incomplete
        if not data.get("last"):
            reasons.append("stale/missing data: no last price for instrument")

    # No chase: current last vs plan entry
    planned = _planned_entry(action)
    try:
        last = float(data.get("last")) if data.get("last") is not None else None
    except (TypeError, ValueError):
        last = None
    slip_lim = float(getattr(config, "KILL_ENTRY_SLIP_PCT", 0.005))
    details["planned_entry"] = planned
    details["last"] = last
    if planned is not None and last is not None and planned > 0:
        dev = abs(last - planned) / planned
        details["entry_deviation"] = dev
        if dev > slip_lim + 1e-12:
            reasons.append(
                f"no chase: last {last:g} vs plan entry {planned:g} ({dev:.2%} > {slip_lim:.2%})")

    # Correlation group risk budget
    side = 1 if action.get("side") == "long" else -1
    group = correlation_group(inst)
    details["correlation_group"] = group
    new_risk = float(planned_risk_usdt) if planned_risk_usdt is not None else _open_risk_usdt(action, snapshot)
    details["new_risk_usdt"] = new_risk
    equity = float(((snapshot or {}).get("account") or {}).get("equity_usdt") or 0)
    if not config.DEMO_USE_FULL_EQUITY and equity:
        equity = min(equity, config.LIVE_EQUITY_CAP)
    unit = float(getattr(config, "CORRELATION_GROUP_MAX_RISK_UNITS", 1.0))
    rpt = float(config.SPOT_RISK_PER_TRADE if action.get("market") == "spot" else config.RISK_PER_TRADE)
    budget = equity * rpt * unit if equity > 0 else 0.0
    existing = _group_side_risk(state, snapshot, group, side, unit_budget=budget)
    reserved = 0.0
    if reserved_groups:
        reserved = float(reserved_groups.get((group, side), 0) or 0)
    details.update(existing_group_risk=existing, reserved_group_risk=reserved, group_budget=budget)
    if budget > 0 and existing + reserved + new_risk > budget + 1e-6:
        reasons.append(
            f"correlation veto: group {group} side={'long' if side == 1 else 'short'} "
            f"risk {existing + reserved + new_risk:.2f} > budget {budget:.2f} "
            f"(existing {existing:.2f} + reserved {reserved:.2f} + new {new_risk:.2f})")

    decision = "VETO" if reasons else "APPROVE"
    return {"decision": decision, "reasons": reasons, "details": details}
