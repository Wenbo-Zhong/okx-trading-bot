# Risk / kill switch / risk gate (DEMO)

Independent of the planner. Soft pauses **block new opens only**; existing positions keep their
exchange-side stops. Hard halts flatten (existing design).

## Hard circuit breakers (`okxbot/risk.py` → `guardrail_status`)

| Trigger | Threshold | Effect |
|---------|-----------|--------|
| Daily loss | `DAILY_LOSS_LIMIT` = **2%** of day-start equity (was 10%) | `day_locked`, `can_open=false`, `must_flatten=true` until next Asia/Shanghai day |
| Max drawdown | `MAX_DRAWDOWN` = **40%** from peak | `halted`, flatten + halt file |

## Soft kill pauses (block opens; no force-close)

| Trigger | Config | Effect |
|---------|--------|--------|
| Consecutive losing closed strategy trades | `KILL_CONSEC_LOSSES=3`, pause `KILL_CONSEC_PAUSE_H=24` h | `state.kill.consec_pause_until`; streak from `note_strategy_close` (maintain reconcile / journal) |
| Entry fill slippage | `KILL_ENTRY_SLIP_PCT=0.005` (0.5%) vs planned entry | `state.kill.slip_pause_until` via `note_entry_slippage` after fill |

Intents counted toward consecutive losses: `KILL_STRATEGY_INTENTS` (smoke/manual excluded).

## Independent risk gate (`okxbot/risk_gate.py`)

Every planned **open** must pass `risk_gate.check_open` inside `validate_action` before
`--execute` can place an order. Returns `APPROVE` / `VETO` with reasons; logged on the decision
journal action row as `risk_gate`.

Checks (all configurable):

1. Active kill / day-lock / halt entry blocks.
2. **Stale data**: last closed candle age > `KILL_STALE_DATA_S` (default 900 s) → VETO.
3. **No chase**: `|last - plan_entry| / plan_entry` > `KILL_ENTRY_SLIP_PCT` → VETO.
4. **Correlation group budget**: instruments share a bucket (e.g. BTC/ETH/SOL/… → `crypto_majors`;
   QQQ+SPY → `us_index`; CL+BZ → `crude`; XAU → `gold`). Same-direction open risk in a group
   counts against one budget = equity × `RISK_PER_TRADE` × `CORRELATION_GROUP_MAX_RISK_UNITS`
   (default 1.0). Exceeded → VETO. Multiple opens in one plan accumulate via `reserved_groups`.

## Visibility

- `scripts/report.py` prints kill lines and `can_open`.
- Snapshot `guardrails.kill` / `entry_blocks` / kill knobs.
- Maintain `ALERTS:` includes `type=kill` when pauses are active.
- Decision journal action rows include `risk_gate`.

DEMO only — live trading remains disabled in this build (`config.DEMO`).
