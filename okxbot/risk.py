"""Position sizing and account-level guardrails / kill-switch pauses."""
import datetime as dt
import math
import time
from zoneinfo import ZoneInfo

from okxbot import config

SH = ZoneInfo(config.TIMEZONE)


def size_position(equity, entry, stop):
    """Return (notional_usdt, leverage, margin) respecting: risk per trade, max leverage,
    max 20% equity margin per position."""
    stop_pct = abs(entry - stop) / entry
    notional = equity * config.RISK_PER_TRADE / stop_pct
    max_notional = equity * config.MAX_MARGIN_PCT * config.MAX_LEVERAGE
    notional = min(notional, max_notional)
    lev = max(1, math.ceil(notional / (equity * config.MAX_MARGIN_PCT) - 1e-9))
    lev = min(lev, config.MAX_LEVERAGE)
    margin = notional / lev
    assert lev <= config.MAX_LEVERAGE and margin <= equity * config.MAX_MARGIN_PCT + 1e-6
    return notional, lev, margin


def _kill(state):
    k = state.setdefault("kill", {})
    if not isinstance(k, dict):
        k = {}
        state["kill"] = k
    return k


def _now_ts(now=None):
    if now is None:
        return time.time()
    if isinstance(now, (int, float)):
        return float(now)
    if isinstance(now, dt.datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=dt.timezone.utc)
        return now.timestamp()
    raise TypeError("now must be timestamp or datetime")


def clear_expired_pauses(state, now=None):
    """Drop kill pauses whose until-ts has passed. Returns list of cleared pause keys."""
    k = _kill(state)
    now_s = _now_ts(now)
    cleared = []
    for key in ("consec_pause_until", "slip_pause_until"):
        until = k.get(key)
        if until is not None and float(until) <= now_s:
            k.pop(key, None)
            reason_key = "consec_pause_reason" if key.startswith("consec") else "slip_pause_reason"
            k.pop(reason_key, None)
            cleared.append(key)
    return cleared


def active_entry_blocks(state, now=None):
    """Reasons that currently block NEW opens (positions keep exchange stops). Empty => ok."""
    clear_expired_pauses(state, now)
    reasons = []
    if state.get("halted"):
        reasons.append("HALTED: " + str(state.get("halt_reason") or ""))
    if state.get("day_locked"):
        reasons.append("daily loss limit hit")
    k = _kill(state)
    now_s = _now_ts(now)
    if k.get("consec_pause_until") and float(k["consec_pause_until"]) > now_s:
        reasons.append(k.get("consec_pause_reason") or
                       f"consecutive losses pause until {dt.datetime.fromtimestamp(float(k['consec_pause_until']), SH).isoformat()}")
    if k.get("slip_pause_until") and float(k["slip_pause_until"]) > now_s:
        reasons.append(k.get("slip_pause_reason") or
                       f"entry slippage pause until {dt.datetime.fromtimestamp(float(k['slip_pause_until']), SH).isoformat()}")
    return reasons


def guardrail_status(state, equity, now=None):
    """Updates peak/day-start in state and returns (can_open, must_flatten, reason).

    must_flatten is True for hard daily-loss / drawdown halt (existing positions flattened by
    maintain/bot). Soft kill pauses (consec losses, slippage) only set can_open=False.
    """
    peak = max(state.get("peak_equity") or equity, equity)
    state["peak_equity"] = peak
    if state.get("halted"):
        return False, True, "HALTED: " + state.get("halt_reason", "")
    dd = 1 - equity / peak if peak > 0 else 0
    if dd >= config.MAX_DRAWDOWN:
        state["halted"] = True
        state["halt_reason"] = f"drawdown {dd:.1%} from peak {peak:.2f} >= {config.MAX_DRAWDOWN:.0%}"
        return False, True, "HALTED: " + state["halt_reason"]
    day_start = state.get("day_start_equity") or equity
    day_pnl = equity / day_start - 1 if day_start > 0 else 0
    if day_pnl <= -config.DAILY_LOSS_LIMIT:
        state["day_locked"] = True
    if state.get("day_locked"):
        return False, True, f"daily loss limit hit ({day_pnl:.1%} today); no trading until next day"
    blocks = active_entry_blocks(state, now)
    # active_entry_blocks already includes halted/day_locked; filter to soft pauses only for message
    soft = [r for r in blocks if not r.startswith("HALTED") and r != "daily loss limit hit"]
    if soft:
        return False, False, "; ".join(soft)
    return True, False, "ok"


def note_strategy_close(state, realized_pnl, intent=None, now=None):
    """Update consecutive-loss kill after a closed strategy trade. Returns pause dict or None."""
    intents = getattr(config, "KILL_STRATEGY_INTENTS", None)
    if intents is not None and intent is not None and intent not in intents:
        return None
    try:
        pnl = float(realized_pnl)
    except (TypeError, ValueError):
        return None
    k = _kill(state)
    if pnl >= 0:
        k["consec_losses"] = 0
        return None
    n = int(k.get("consec_losses") or 0) + 1
    k["consec_losses"] = n
    need = int(getattr(config, "KILL_CONSEC_LOSSES", 3))
    if n < need:
        return None
    hours = float(getattr(config, "KILL_CONSEC_PAUSE_H", 24))
    until = _now_ts(now) + hours * 3600
    reason = f"KILL consecutive losses: {n} losing closes; pause new entries {hours:g}h"
    k["consec_pause_until"] = until
    k["consec_pause_reason"] = reason
    k["consec_losses"] = 0  # reset streak after arming pause
    k["last_kill"] = {"kind": "consec_losses", "at": _now_ts(now), "until": until, "reason": reason}
    return {"kind": "consec_losses", "until": until, "reason": reason}


def note_entry_slippage(state, planned_px, fill_px, inst_id=None, now=None):
    """If |fill-planned|/planned > KILL_ENTRY_SLIP_PCT, pause new entries. Returns pause or None."""
    try:
        planned, fill = float(planned_px), float(fill_px)
    except (TypeError, ValueError):
        return None
    if planned <= 0 or fill <= 0:
        return None
    slip = abs(fill - planned) / planned
    lim = float(getattr(config, "KILL_ENTRY_SLIP_PCT", 0.005))
    if slip <= lim + 1e-12:
        return None
    hours = float(getattr(config, "KILL_CONSEC_PAUSE_H", 24))
    until = _now_ts(now) + hours * 3600
    label = f"{inst_id} " if inst_id else ""
    reason = (f"KILL entry slippage: {label}fill {fill:g} vs planned {planned:g} "
              f"({slip:.2%} > {lim:.2%}); pause new entries {hours:g}h")
    k = _kill(state)
    k["slip_pause_until"] = until
    k["slip_pause_reason"] = reason
    k["last_kill"] = {"kind": "entry_slippage", "at": _now_ts(now), "until": until,
                     "reason": reason, "slip": slip, "instId": inst_id}
    return {"kind": "entry_slippage", "until": until, "reason": reason, "slip": slip}


def kill_status_lines(state, now=None):
    """Human-readable kill / pause lines for report/maintain."""
    clear_expired_pauses(state, now)
    lines = []
    if state.get("halted"):
        lines.append("HALTED: " + str(state.get("halt_reason") or ""))
    if state.get("day_locked"):
        lines.append(f"daily loss lock active (limit {config.DAILY_LOSS_LIMIT:.0%} of day-start)")
    k = _kill(state)
    now_s = _now_ts(now)
    if int(k.get("consec_losses") or 0):
        lines.append(f"consecutive losses streak: {k['consec_losses']}/{config.KILL_CONSEC_LOSSES}")
    if k.get("consec_pause_until") and float(k["consec_pause_until"]) > now_s:
        until = dt.datetime.fromtimestamp(float(k["consec_pause_until"]), SH).strftime("%Y-%m-%d %H:%M %Z")
        lines.append(f"entries paused (consec losses) until {until}: {k.get('consec_pause_reason', '')}")
    if k.get("slip_pause_until") and float(k["slip_pause_until"]) > now_s:
        until = dt.datetime.fromtimestamp(float(k["slip_pause_until"]), SH).strftime("%Y-%m-%d %H:%M %Z")
        lines.append(f"entries paused (slippage) until {until}: {k.get('slip_pause_reason', '')}")
    return lines
