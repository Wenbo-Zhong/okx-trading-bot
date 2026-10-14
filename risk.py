"""Position sizing and account-level guardrails."""
import math
import config


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


def guardrail_status(state, equity):
    """Updates peak/day-start in state and returns (can_open, must_flatten, reason)."""
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
    return True, False, "ok"
