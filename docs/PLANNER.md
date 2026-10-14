# Planner mode (since 2026-10-14)

**Roles.** An external LLM planner (the trading routine) makes every trade decision from news and
K-lines. This repo only (1) collects information into a machine-readable snapshot, (2) validates and
executes *explicit* plans safely, and (3) runs automatic safety maintenance. Keyword news sentiment no
longer gates anything; C7 is only a reference signal in the snapshot and the backtest baseline
(`backtests/`). All `AUTO_*` flags in `okxbot/config.py` are `False`.

All commands run from the project root. DEMO only (`config.DEMO = True`).

| Command | Writes | Orders? |
|---|---|---|
| `python3 -m scripts.maintain [--dry-run]` (also `python3 -m scripts.run_once`, root `run_once.py`) | state, equity, journal | only safety: breaker flatten, missing-stop fill, recovery |
| `python3 -m scripts.snapshot [--inst ID ...] [--no-news] [--brief\|--view] [--out PATH]` | `data/snapshots/<id>.json`, `latest.json` | never |
| `python3 -m scripts.execute_plan PLAN.json [--snapshot PATH]` | decision journal | **never (dry-run default)** |
| `python3 -m scripts.execute_plan PLAN.json --execute` | state, journal | yes, per plan |
| `python3 -m scripts.execute_plan --no-trade --snapshot-id ID --reason TEXT [--uncertainty TEXT]...` | decision journal | never |
| `python3 -m scripts.decisions [--last N] [--json]` | – | never |
| `python3 -m scripts.report` | – | never |

`maintain` and `execute_plan` share `data/run_once.lock`: maintain exits 3 if busy; execute_plan waits up to 60 s.

## What runs automatically (maintain) vs. needs a plan

Automatic, every cycle: reconcile positions the exchange closed (SL/TP hits are journaled); recover
pending order intents by `ordId`/`clOrdId` (spot, planner opens) without resubmitting; account circuit
breaker (daily loss `DAILY_LOSS_LIMIT`, drawdown `MAX_DRAWDOWN` → flatten all + halt); stop coverage:
every position must have an exchange SL covering its full size — a missing/short SL is placed at the
recorded stop (or, for an unknown position, at the existing exchange stop / a MAX_STOP_PCT fallback);
partial-fill remainders stay protected; equity log, journal refresh, `ALERTS:` section.
Exchange-side SL/TP orders execute on the exchange by themselves.

Never automatic (needs a plan action): opening, closing (trend exits removed), moving a stop
(trailing and liquidation-cluster nudges removed), changing TP, cancelling orders, BTC/ETH/OKB sleeve
trades. An existing exchange stop is authoritative and is never moved by maintain.

## Snapshot `okxbot.snapshot/v1`

Top level: `schema`, `snapshot_id` (`snap-YYYYmmddTHHMMSSZ-<10 hex>`), `sha256` (canonical JSON without
id/hash), `generated_at` (UTC), `generated_at_local`, `demo`, `account_available`, `account`,
`guardrails`, `instruments`, `news`, `events`, `c7_reference`, `completeness`
(`account`/`market`/`news`/`events` booleans), `errors`.

- `account`: `equity_usdt`, `avail_usdt`, `positions[]` (`instId, market, side, size, avg_px, last, mark,
  upl, lev, margin, liq_px, sl{algoId,trigger}|null, tp{algoId,trigger}|null, state_record`),
  `spot_balances{ccy:{eq,avail}}`, `pending_orders[]`, `pending_algos[]`. `null` without API keys.
- `guardrails`: `can_open, must_flatten, status, day_start_equity, peak_equity, halted` and the limits
  `MAX_LEVERAGE, MAX_MARGIN_PCT, MAX_POSITIONS, RISK_PER_TRADE, DAILY_LOSS_LIMIT, MAX_DRAWDOWN,
  MIN_STOP_PCT, MAX_STOP_PCT`.
- `instruments{instId}`: `market, spec{tickSz,lotSz,minSz,ctVal,maxLever}, last, mark, index,
  funding{rate_8h,next_time}, fees{maker,taker,source}, orderbook{bid,ask,spread_bps,
  depth_usdt_within_0.5pct_bid/ask}, candles{"15m","1H","4H","1D":{bars[[ts_ms,o,h,l,c,vol]],
  indicators{ema20,ema50,ema200,atr14,rsi14,ret_24h,ret_7d}}}, c7{signal,strength,stop,tp,reason},
  liq_clusters, liquidity_ok`. Bars are CLOSED bars only (48×15m, 48×1H, 60×4H, 30×1D).
  `--brief` prints the same snapshot without raw `bars` (adds `last_bar`, `n_bars`); the saved file is full.
- `news[]` (last 24h, newest first, ≤60, deduplicated by URL/title): `id, title, source, url,
  published_at, age_h, summary, tags`. No sentiment score.
- `events[]`: macro events in the next 7 days with source URLs. `c7_reference[]`: `{instId, side, strength}`.

Default universe: held instruments + the bot's top-volume swap/spot universe + tradfi list (~55
instruments, ~2–3 min, ~2 MB). Use `--inst` to focus.

## Plan `okxbot.plan/v1`

```json
{
  "schema": "okxbot.plan/v1",
  "plan_id": "2026-10-14T2140-01",
  "snapshot_id": "snap-20261014T133021Z-f5884a33b9",
  "created_at": "2026-10-14T21:40:00+08:00",
  "valid_until": "2026-10-14T22:10:00+08:00",
  "rationale": "ETH reclaimed 4H EMA50 on rising volume; no high-impact macro before 02:00.",
  "uncertainties": ["CPI tomorrow", "news coverage thin for ETH"],
  "actions": [
    {"action": "open", "instId": "ETH-USDT-SWAP", "market": "swap", "side": "long",
     "size": {"risk_usdt": 300}, "leverage": 3, "stop": 2410.0, "take_profit": 2620.0,
     "order_type": "market", "rationale": "breakout retest"},
    {"action": "amend_sl", "instId": "CL-USDT-SWAP", "stop": 93.10},
    {"action": "close", "instId": "OP-USDT-SWAP", "fraction": 0.5, "reason": "take half into resistance"},
    {"action": "hold", "instId": "XAU-USDT-SWAP", "reason": "thesis intact"}
  ]
}
```

Actions:
- `open`: `instId, market ("swap"|"spot"), side ("long"|"short"; spot long only), size` = exactly one of
  `{"risk_usdt"}` / `{"notional_usdt"}` / `{"contracts"}`, `leverage` (int, swap, default 1), `stop`
  (required, absolute), `take_profit` (optional), `order_type` (`market` default | `limit` + `limit_px`).
- `close`: `instId, reason`, and `fraction` (0<f≤1, default 1) **or** `size` (base qty / contracts).
- `amend_sl`: `instId, stop`. `amend_tp`: `instId, take_profit` (`null` removes the TP).
- `cancel`: `instId` and exactly one of `ordId` / `clOrdId` / `algoId` (must be pending on that instrument).
- `hold`: `reason`, optional `instId`. Recorded only.

Validation. Plan level (any failure → nothing executes): schema/required fields; `created_at ≤ now ≤
valid_until`; validity window ≤ 6 h; snapshot file exists, id matches and is ≤ `PLAN_MAX_SNAPSHOT_AGE_MIN`
(45) min old (re-checked before every action when executing); `plan_id` not already executed; ≤
`PLAN_MAX_ACTIONS` (6). `open`: guardrails `can_open` (breaker/daily lock/halt) and no unresolved order
intent; instrument live and liquidity filter OK; no position/pending order in the same coin across spot
and swap (also within the plan); `MAX_POSITIONS`; stop on the loss side, distance between `MIN_STOP_PCT`
and `MAX_STOP_PCT`, non-marketable; TP on the profit side; leverage ≤ `MAX_LEVERAGE` and instrument cap;
risk ≤ equity × `RISK_PER_TRADE` (spot `SPOT_RISK_PER_TRADE`) — `risk_usdt` above the cap is clamped and
noted, other size forms above it are rejected; margin ≤ `MAX_MARGIN_PCT`; then exchange/affordability/
depth clamps and min size. Execution: write-ahead order intent, entry with attached SL/TP, stop coverage
verified (if the stop cannot be confirmed the position is closed immediately). `close`: tracked position
must exist; partial closes use actual fills and the remainder keeps a full-size stop. `amend_sl`/`amend_tp`:
new trigger must be on the right side and not marketable (rejected, not silently clamped), loss
distance ≤ `MAX_STOP_PCT` from current price; amendment is confirmed on the exchange.

Result (stdout, one JSON): `plan_id, snapshot_id, mode ("dry_run"|"execute"), valid, plan_errors[],
actions[{index, action, instId, status ("rejected"|"would_execute"|"executed"|"failed"|"skipped"),
reasons[], order, result}]` plus the journal fields below. Exit code 0 only if valid and no action
was rejected/failed.

If information is insufficient or the plan expired: do not trade — journal a `--no-trade` decision.

## Decision journal `data/decision_journal.jsonl`

One line per call: `ts, kind ("plan"|"no_trade"), plan_id, snapshot_id, snapshot_sha256, plan_sha256,
mode, valid, valid_until, rationale, uncertainties, actions[...], outcome{counts, order_ids, traded},
errors[]` (plus an earlier `claimed: true` line when an execution starts). `scripts.decisions` joins
order ids with `journal_events.jsonl` closes (realized PnL).
