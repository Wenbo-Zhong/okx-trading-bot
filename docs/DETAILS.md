# OKX demo trading bot

Autonomous bot for Wenbo's OKX **demo** account. Reads 1h/4h K-lines + public crypto news RSS,
decides entries, places orders with stop-loss/take-profit, manages trails and guardrails.

**Always demo** (`config.DEMO = True` → header `x-simulated-trading: 1`). No withdrawal endpoints.

## Layout
```
okx-bot/
  run_once.py      # one decision cycle (schedule this)
  report.py        # equity / positions / PnL
  backtest.py      # historical technical backtest
  bot.py           # cycle logic
  strategy.py      # signals + stops
  risk.py          # sizing + guardrails
  news.py          # RSS sentiment
  okx_client.py    # REST client (endpoint whitelist)
  config.py        # guardrails + strategy params
  BACKTEST.md      # measured backtest results (honest)
  data/            # state.json, decisions.jsonl, trades.csv, equity.csv
```

## Credentials
```bash
export OKX_API_KEY=...
export OKX_API_SECRET=...
export OKX_API_PASSPHRASE=...
```
Demo Trading API keys only. Permissions: read + trade (never withdraw).

## Run
```bash
cd /workspace/okx-bot
python run_once.py    # fetch → decide → place/manage → log
python report.py      # print equity / open positions / PnL
python backtest.py    # re-run technical backtest (writes data/backtest_*)
```

Dry-run (no keys): same `run_once.py` — public data + decisions logged, no orders.

## Schedule (every hour, a few minutes after the candle close)
```bash
# cron (UTC+8 box clock). OKX 1h candle closes at :00; give it ~2 minutes.
mkdir -p /workspace/okx-bot/data/logs
crontab -l 2>/dev/null | grep -v okx-bot; cat <<'CRON'
2 * * * * cd /workspace/okx-bot && /usr/bin/python3 run_once.py >> data/logs/cron.log 2>&1
5 * * * * cd /workspace/okx-bot && /usr/bin/python3 report.py >> data/logs/report.log 2>&1
CRON
# Or with systemd timer / your preferred scheduler. Env vars must be available to the job
# (put them in the crontab line, a file sourced by the job, or the service unit).
```

## Guardrails (enforced in code)
| Rule | Value |
|---|---|
| Max leverage | 5x (SWAP); spot fallback is 1x |
| Max margin / position | 20% of equity |
| Max concurrent positions | 3 |
| Stop-loss | required at every entry (attached algo; re-placed if missing) |
| Daily loss limit | −10% day-start equity → flatten + no new trades until next day (UTC+8) |
| Max drawdown | −40% from peak → halt (flag file `data/HALTED`) |
| Universe | top ~30 liquid crypto USDT instruments |
| Withdrawals | never called (client whitelist) |

## Equity / sizing
- **Demo now:** `DEMO_USE_FULL_EQUITY = True` → size off live `totalEq` (whole demo account).
- **Later (live 500U):** set `DEMO_USE_FULL_EQUITY = False`; sizing caps at `LIVE_EQUITY_CAP = 500`.

## Account mode (important)
Demo is on **Futures mode** (`acctLv=2`, `posMode=long_short_mode`). Multi-currency (`acctLv=3`)
could not be enabled via API (OKX 51070) — turn it on in the Demo UI if you want BTC/ETH/OKB to
count as swap margin. Until then, USDT-margined swaps pull margin from the **USDT balance (~5000)**
even though sizing/`report` use full `totalEq` (~102k); entries are refused if required margin
exceeds 95% of available USDT.

If the account falls back to **Spot** mode, the bot trades **SPOT long-only (1x)** instead of swaps.

## Strategy (default C7 — see BACKTEST.md, round 2)
1h trend filter (close & EMA50 vs EMA200) + 12h Donchian breakout, no MACD/RSI filters.
Stop 3×ATR(1h), take-profit 2R, chandelier trail 3×ATR after +1R, exit on 1h trend flip, risk 0.5%/trade.
Backtest: ~2.25 trades/day over the 30-instrument universe, PF ≈1.1. The edge is thin and cost-sensitive.
Previous default V7 (168h breakout, 4h trend) is documented in BACKTEST.md round 1.


## Spot module (DEMO mandate 2026-10-10) — `spot.py`, `spot_alloc.py`
Runs inside every `run_once.py` cycle, **before** swap entries (so USDT freed by sells is usable as swap margin):
1. **Allocation sleeves** for `SPOT_ALLOC_COINS` (BTC, ETH, OKB). Sell the whole holding to USDT when the 4h AND 1h trends are
   both down. Buy back with the sleeve's USDT when both are up. Switches are at least 24 h apart and dust under 10 USDT is skipped.
   Sells are allowed even under guardrail locks (they reduce risk); rebuys are not. State: `state.json -> spot.sleeves`.
2. **Spot C7 longs** (up to `SPOT_MAX_LONGS`=2) on top-20 liquid USDT spot pairs. Sleeve coins, stablecoins and coins with
   an open swap are excluded. Risk 0.5%, notional ≤20% of equity, no leverage or borrowing. An OCO stop/TP is placed right
   after the fill (the position is sold if that fails). Stops trail; exit on trend flip; flattened by guardrails.
Settings: `config.py` (`SPOT_*`). Backtest: `python3 backtest_spot.py` (BACKTEST.md round 3).


## Alerts, funding and event/news risk (2026-10-10)
- `python3 alerts.py` runs standalone. `run_once.py` always ends with an `ALERTS:` section (empty if none).
  Latest alerts are written to `data/alerts.json`, the history to `data/alerts.jsonl`, and the cooldown state to `data/alerts_state.json` (4h default; funding 8h, events 12h, news 24h).
  - Price: BTC/ETH ±3% in 1h or ±6% in 4h; held/open instruments (swaps, spot holdings, spot longs) ±5% in 1h or ±10% in 4h (from 5m candles).
  - Funding: open swaps PAYING more than 0.05%/8h (8h-equivalent).
  - Events: high-impact macro events in the next 48h, plus an active "entries blocked" window.
  - News: high-impact negative headlines (systemic keywords; hacks/exploits/delistings only if they hit a major venue/asset or are ≥$100M).
- `funding.py`: one call to `/public/funding-rate?instId=ANY`. New swap entries are skipped if that side would pay >0.05%/8h (`FUNDING_MAX_PAY_8H`).
- `events.py` + `data/events.json`: FOMC (federalreserve.gov), CPI and NFP (bls.gov), each with its source URL.
  `python3 events.py --refresh` re-reads the Fed calendar (BLS blocks scripted downloads, so CPI/NFP are maintained by hand).
  New entries (swap, spot longs, sleeve rebuys) are blocked from 2h before to 30 min after an event; new size is halved 2–6h before.
- `news.py`: `high_impact` items in the last 6h block new longs (swap longs, spot longs, sleeve rebuys).

## US stock / commodity perps (added 2026-10-10)
`tradfi.py` + config `TRADFI_*`. Every cycle the bot checks each of
MU CRCL NVDA QQQ MRVL PLTR DELL ORCL MSFT HOOD CRWV ASML LLY XAU CL BZ XAG. An instrument joins the SWAP entry
universe only if it is live on demo, its demo mark price is within 2% of the production mark, and its production
24h volume is at least 2M USDT. Sizing is 0.5x crypto risk (0.25%), and 0.25x on weekends and outside US cash hours for stocks.
Leverage cap (5x), the 20% margin cap, stop-loss on every position and the shared 3-position swap cap are all unchanged.
Contract value comes from ctVal (1 share for stocks; XAU 0.001, CL 0.1, BZ/XAG 0.01). The funding filter uses the 8h-normalised rate.
US holidays are not modelled. Backtest: see BACKTEST.md Round 6 (negative on stocks).

## OKX trading bots (grid / DCA / recurring) on demo
`tests/tradingbot_probe.py` (read | grid). These endpoints are NOT in the main bot's whitelist. See the report in data/tradingbot_probe_*.json.

## Estimated liquidation clusters (reference only, added 2026-10-10)
`liqmap.py`, with config `LIQMAP_*`. There is no free heatmap API (the CoinGlass heatmap needs the paid Professional plan),
so the map is estimated from OKX public hourly OI plus 1h candles (see BACKTEST.md Round 7). It's used in two places:
(a) when a new swap entry is placed, the stop is moved beyond a dense cluster sitting near it (max MAX_STOP_PCT; sizing uses the new stop, TP unchanged);
(b) the `liq_cluster` alert covers BTC, ETH and held swaps, flags clusters within ±2%, and notes whether our stop is inside one.
It is never an entry signal. Existing and trailing stops are not nudged.
Since 15:34 on 2026-10-10 the nudge also covers OPEN swaps and trailing stops (`LIQMAP_NUDGE_OPEN`). Each cycle, after the trail
level is computed: if the stop is inside or within 0.5 ATR of a dense estimated cluster, it moves to the far side + 0.25 ATR.
The move is skipped if the max loss would exceed 1.5x the original planned risk (`LIQMAP_MAX_RISK_MULT`), if it exceeds MAX_STOP_PCT, or if it would trigger immediately.
The stop is changed with amend-algos and then verified on the exchange. The fallback places a new SL first and only then cancels the old one.
Every change is logged to trades.csv (event=stop_nudge) and shown under ALERTS as [stop_adjust].

## Trade journal (2026-10-10)
`python3 journal.py` writes `data/journal.csv` (one row per round trip, plus spot-allocation events) and `data/journal.md` (Chinese review).
`run_once.py` also refreshes both after every cycle. Sources:
* `data/journal_events.jsonl`: open (with the entry rationale captured at order time), stop (trail / liq_nudge), close_request, close (from OKX positions-history), and alloc events. Written by hooks in bot.py and spot.py.
* Live positions (unrealized PnL, fees, funding) and production 15m candles (MFE/MAE).
Trades before 2026-10-10 17:30 were backfilled by `journal_backfill.py`; their rationale is marked reconstructed.

## Spot preference for ~1x longs + pre-order affordability checks (2026-10-12)
- **Spot preference** (`SPOT_PREFER_FOR_LOW_LEV_LONGS`, `SPOT_PREFER_MAX_LEV=1`): when a swap LONG signal sizes to
  1x leverage and the coin has a live `XXX-USDT` spot pair (and isn't a sleeve coin BTC/ETH/OKB or tradfi),
  the bot buys spot instead (`SpotManager.open_long`, same risk sizing, OCO stop+TP attached, no leverage).
  Shorts, tradfi (XAU/QQQ/...) and genuinely levered longs (>1x) stay swaps. Routed spot longs count toward
  `MAX_POSITIONS` (not the separate `SPOT_MAX_LONGS`), and a coin held as spot is not also opened as a swap.
  Liq-map stop nudge does not apply to routed spot longs (the map is swap-OI based).
- **Pre-order clamps** (`sizing.py`): before every order, size = min(risk size, affordable size, exchange cap,
  market-order cap), rounded down to lot; skip if below minimum.
  Swap: after `set-leverage`, `GET /api/v5/account/max-size` (maxBuy/maxSell already includes position-tier caps
  at the current leverage + existing position + pending orders, e.g. ZRO 620 ct at 1x) and margin =
  `availEq` x lev; instrument `maxMktSz`. Spot: USDT `availBal`, `max-size` maxBuy, `maxMktAmt`, `minSz`.
  95% buffer (`ORDER_SIZE_BUFFER`). Whitelist gained two read-only GETs: `/account/max-size`, `/account/max-avail-size`.
- Tests: `python3 tests/test_sizing.py` (10 tests incl. ZRO 51004 and GRASS 51008 cases, mocked bot routing).

## Funding-harvest exception (2026-10-12; **disabled 2026-10-14**)
- **Off by default:** `FUNDING_HARVEST_ENABLED = False`. Spot preference only (`SPOT_PREFER_FOR_LOW_LEV_LONGS`).
  Backtest (`bt_harvest.py`) showed ~11 harvest trades/year, win rate ~18%, net loss on price; live demo never
  opened one. Set the flag True to re-enable without code changes.
- When enabled: if a long would be ~1x (normally routed to spot) but the swap's current 8h-equivalent funding is
  <= -`FUNDING_HARVEST_MIN_8H` (0.02%/8h ≈ 22%/yr received by longs) AND the last settled rate was <= 0, the bot
  opens the 1x SWAP long instead, `intent="funding_harvest"` (state, trades.csv reason, journal). The swap round
  trip is also 0.10% cheaper than spot, so no extra fee hurdle; the threshold keeps it to clearly negative funding.
- While held (each cycle): funding >= +0.01%/8h counts as flipped. After `FUNDING_HARVEST_FLIP_CYCLES`=6 consecutive
  cycles (~3h) it is flagged in the log/alerts; it is closed (reason `funding_flip`) only if still >=
  `FUNDING_HARVEST_CLOSE_8H` (0.05%/8h, the entry-block level). Stop/TP/trailing/liq-nudge are unchanged.
- Journal: `intent` column, 【吃资金费率】 tag, a dedicated summary table, and per trade PnL split
  价格 / 资金费 / 手续费. Funding = OKX `fundingFee` (positions / positions-history), with a fallback to
  funding bills (`GET /account/bills type=8`, read-only, ~7 days).
- Tests: `cd tests && python3 test_harvest.py` and `python3 test_sizing.py`.

## Fill accounting + thin-book protection (2026-10-12)
- After every market order (spot and swap), the bot re-queries the order and logs/journals the ACTUAL `accFillSz`,
  `avgPx`, fee and notional. Stop/TP qty and planned risk use the filled (net-of-fee) qty. A fill < 98% of the
  request logs a `PARTIAL FILL` event (decisions log + `partial_fill` journal event). Swaps: if the attached SL
  covers less than the filled size, a protective SL is added for the rest.
- Spot market buys are capped at 80% of the DEMO ask depth within 1% of mid (`SPOT_DEPTH_MAX_SLIP`).
  Routed ~1x longs whose depth cap is < 50% of planned size (`SPOT_DEPTH_FALLBACK_FRAC`) fall back to the 1x
  swap (`intent=spot_too_thin`). The demo spot books are much thinner than production
  (ZRO: ~450 vs ~31k USDT within 1%).
- Tests: `cd tests && python3 test_fills.py`.

## Liquidity filter + stop-slippage buffer (2026-10-12)
Before every swap or spot entry, `Bot.liquidity_check` reads the DEMO order book (`/market/books`, 400 levels):
1. **Slippage buffer:** expected stop slippage = max(floor, impact of a market order of our size on the
   STOP side: asks for shorts, bids for longs). Floor is 0.5% for majors (`SLIP_MAJORS`) and 1.0% otherwise.
   Size is shrunk so qty × (|entry − stop| + slippage) = planned risk.
2. **Liquidity filter:** both the entry-side AND stop-side depth within 1.5% of mid (`LIQ_DEPTH_PCT`) must be
   >= 1.0× (`LIQ_MIN_COVER`) the shrunk notional; otherwise the entry is skipped. That decision is logged
   (`skipped: thin book`) and journaled as a `skip` event (journal.md section 因盘口太薄跳过的信号).
   A routed ~1x spot long that fails falls back to the 1x swap check. Book fetch failure = skip.
The contract-spec sanity check (≤1.2× plan) still runs after this. Tests: `cd tests && python3 test_liquidity.py`.
