# Backtest — technical strategy only (news filter NOT included)

Measured on 2026-10-09 (UTC+8) with `python3 -m backtests.backtest 8760` and `python3 -m experiments.experiments`.
Raw outputs: `data/backtest_final.txt`, `data/experiments.txt`, `data/experiments.csv`, `data/backtest_trades.csv`.

## Setup
- Data: OKX **production** public 1h candles (`/api/v5/market/history-candles`), last 8760 bars per instrument,
  BTC, ETH, SOL, XRP, DOGE, SUI USDT perpetuals. First 300 bars are indicator warm-up, so the
  evaluated window is **2025-10-21 17:00 UTC → 2026-10-09 03:00 UTC** (≈ 11.5 months).
- Market context in that window (buy & hold from the same start): BTC −26.7%, ETH −37.9%, SOL −43.2%,
  XRP −44.0%, DOGE −58.4%, SUI −60.2% — a bear / choppy market.
- One shared 500 USDT portfolio, same code path as the live bot (signals, sizing, guardrails):
  max 3 positions, max 5x, ≤20% equity margin per position, daily loss limit 10% (flatten + stop for the day),
  40% drawdown from peak → halt for good.
- Fills: entry at the next 1h open after a closed-bar signal; stop/TP checked intrabar (if both touched in the
  same bar, the stop is assumed to hit first — pessimistic). Costs: 0.05% taker fee + 0.03% slippage per side.
  **Funding fees are not modelled.** No compounding tricks; equity is marked at every 1h close.

## Round-1 choice "V7" (superseded on 2026-10-09 19:xx UTC+8 by C7, see Round 2 below)
4h trend filter (close & EMA20 vs EMA50 on 4h) + 1h close breaking the prior **168h (7-day)** high/low,
MACD histogram in the same direction, RSI not extreme. Stop = 8 × ATR(14, 1h) (skipped if > 8% away),
take-profit = 4R, chandelier trail (8 × ATR) after +1.5R, exit if the 4h trend flips. Risk 1% of equity per trade.

| Metric (portfolio, 6 pairs, full window) | Value |
|---|---|
| Start → end equity | 500.00 → 492.67 USDT |
| **Return** | **−1.47%** |
| **Max drawdown** | **11.48%** |
| **Trades** | **61** |
| **Win rate** | **34.4%** |
| Avg win / avg loss | +7.01 / −3.86 USDT |
| Profit factor | 0.95 |
| Exit reasons | stop 36, trend flip 20, take-profit 3, end-of-data 2 |
| Halted by guardrail | no |

Same strategy run on each pair alone (500 USDT each):

| Pair | Return % | Max DD % | Trades | Win % | Profit factor |
|---|---|---|---|---|---|
| BTC-USDT-SWAP | +1.88 | 4.76 | 17 | 29.4 | 1.24 |
| ETH-USDT-SWAP | +2.36 | 3.00 | 10 | 40.0 | 1.47 |
| SOL-USDT-SWAP | −1.52 | 4.99 | 14 | 35.7 | 0.80 |
| XRP-USDT-SWAP | −3.88 | 5.71 | 16 | 25.0 | 0.51 |
| DOGE-USDT-SWAP | −3.70 | 4.30 | 9 | 22.2 | 0.37 |
| SUI-USDT-SWAP | −0.99 | 3.27 | 9 | 33.3 | 0.80 |

## Every variant tried (all reported, not just the best)
IS = 2025-10-21 → 2026-06-01, OOS = 2026-06-01 → 2026-10-09. "FULL" stops trading once the 40% halt fires,
so for halted variants FULL can equal IS.

| Variant | Period | Return % | Max DD % | Trades | Win % | Profit factor | Halted (40% DD) |
|---|---|---|---|---|---|---|---|
| V0 base (20h breakout, 2.5ATR stop, 3R TP, BE@1R, trail@1.5R) | IS 2025-11..2026-05 | 7.09 | 36.0 | 289 | 37.7 | 1.02 | False |
| V0 base (20h breakout, 2.5ATR stop, 3R TP, BE@1R, trail@1.5R) | OOS 2026-06..2026-10 | -23.69 | 40.37 | 89 | 25.8 | 0.7 | True |
| V0 base (20h breakout, 2.5ATR stop, 3R TP, BE@1R, trail@1.5R) | FULL | -4.54 | 40.48 | 346 | 36.4 | 0.99 | True |
| V1 no BE/trail | IS 2025-11..2026-05 | 40.15 | 40.18 | 139 | 33.1 | 1.15 | True |
| V1 no BE/trail | OOS 2026-06..2026-10 | -14.37 | 40.19 | 52 | 25.0 | 0.83 | True |
| V1 no BE/trail | FULL | 40.15 | 40.18 | 139 | 33.1 | 1.15 | True |
| V2 48h breakout | IS 2025-11..2026-05 | 25.06 | 39.36 | 211 | 36.0 | 1.1 | False |
| V2 48h breakout | OOS 2026-06..2026-10 | -23.86 | 40.32 | 85 | 23.5 | 0.7 | True |
| V2 48h breakout | FULL | 5.64 | 40.29 | 272 | 33.5 | 1.02 | True |
| V3 48h breakout, 4ATR stop, no BE | IS 2025-11..2026-05 | -12.43 | 40.73 | 90 | 33.3 | 0.91 | True |
| V3 48h breakout, 4ATR stop, no BE | OOS 2026-06..2026-10 | -19.1 | 40.11 | 60 | 26.7 | 0.78 | True |
| V3 48h breakout, 4ATR stop, no BE | FULL | -12.43 | 40.73 | 90 | 33.3 | 0.91 | True |
| V5 = V3 with 1% risk/trade | IS 2025-11..2026-05 | -17.01 | 30.12 | 123 | 33.3 | 0.82 | False |
| V5 = V3 with 1% risk/trade | OOS 2026-06..2026-10 | -14.12 | 28.85 | 76 | 27.6 | 0.74 | False |
| V5 = V3 with 1% risk/trade | FULL | -18.48 | 36.24 | 197 | 31.5 | 0.86 | False |
| V6 = V1 with 1% risk/trade | IS 2025-11..2026-05 | 2.27 | 30.54 | 218 | 30.3 | 1.01 | False |
| V6 = V1 with 1% risk/trade | OOS 2026-06..2026-10 | -25.95 | 40.05 | 100 | 20.0 | 0.68 | True |
| V6 = V1 with 1% risk/trade | FULL | 6.74 | 40.08 | 296 | 28.4 | 1.02 | True |
| V7 168h breakout, 8ATR stop, 4R TP, trail 8ATR@1.5R, 1% risk | IS 2025-11..2026-05 | 0.88 | 9.88 | 30 | 36.7 | 1.06 | False |
| V7 168h breakout, 8ATR stop, 4R TP, trail 8ATR@1.5R, 1% risk | OOS 2026-06..2026-10 | -2.33 | 11.48 | 31 | 32.3 | 0.86 | False |
| V7 168h breakout, 8ATR stop, 4R TP, trail 8ATR@1.5R, 1% risk | FULL | -1.47 | 11.48 | 61 | 34.4 | 0.95 | False |
| V4 96h breakout, 4ATR stop, 4R TP, trail@2R | IS 2025-11..2026-05 | -10.08 | 40.09 | 79 | 35.4 | 0.91 | True |
| V4 96h breakout, 4ATR stop, 4R TP, trail@2R | OOS 2026-06..2026-10 | -7.84 | 34.65 | 56 | 30.4 | 0.91 | False |
| V4 96h breakout, 4ATR stop, 4R TP, trail@2R | FULL | -10.08 | 40.09 | 79 | 35.4 | 0.91 | True |

## Honest reading
- **No variant showed a robust edge.** Only V4 (+13.96%? see table) and V1/V2 variants differ — see table;
  the ones that made money in-sample (V1 +40%, V2 +25%) hit or nearly hit the 40% drawdown halt and then lost in OOS.
- V7 was picked because it was the most stable (smallest drawdown, IS/OOS both near flat), not because it is
  profitable — its full-window result is a small loss (−1.47%, PF 0.95). Choosing it *after* seeing these numbers
  is itself a selection bias, so treat even −1.5% as optimistic.
- Sample size is small (61 trades); a few trades change the result materially.
- The news-sentiment filter is not backtested (no free historical headline archive was used).
- Demo-environment candles differ from production (demo has its own order book, wicks differ). The bot reads
  signals from production market data but fills/stops happen on demo prices, so demo results will diverge somewhat.

## Discarded runs (for transparency)
Two earlier sweeps were thrown away: one had a timestamp-unit bug that made the 4h trend filter always "down"
(short-only trading), and one accidentally used demo-environment candles. Numbers above are from the fixed code on
production candles.


---

# Round 2 (2026-10-09, evening UTC+8) — looser entries for more trades

Wenbo approved loosening entries so the demo bot trades more often. Guardrails unchanged (5x max, ≤20% margin/position,
3 positions, SL on every entry, −10% daily lock, −40% halt). Same engine, costs (0.05% fee + 0.03% slippage per side,
no funding) and same ~1-year 1h production candles (2025-10-21 → 2026-10-09, IS before 2026-06-01, OOS after).

New: tested on **U30** = the bot's current top-30 crypto USDT-perp universe (MET has 8739 bars, LIT 6944; others full year)
as well as the original **U6** pairs. U30 is chosen by *today's* volume, so it has survivorship bias.
Trades/day = trades ÷ days in the window, portfolio-wide, with the max-3-positions cap applied.

New strategy switches in `okxbot/config.py`: `TREND_TF` ("4h" EMA20/50 on 4h, or "1h" close & EMA50 vs EMA200 on 1h),
`USE_MACD`, `RSI_CAP` (100 = off), `REQUIRE_EMA_ALIGN` (1h EMA20 vs EMA50).

## All round-2 variants (all reported)
| Variant | Universe | Period | Return % | Max DD % | Trades | Trades/day | Win % | PF | Halted |
|---|---|---|---|---|---|---|---|---|---|
| V7 (current) | U30 | IS | -6.66 | 14.6 | 58 | 0.26 | 34.5 | 0.77 | False |
| V7 (current) | U30 | OOS | -6.3 | 16.09 | 44 | 0.34 | 36.4 | 0.73 | False |
| V7 (current) | U30 | FULL | -10.64 | 17.26 | 99 | 0.28 | 34.3 | 0.78 | False |
| V7 (current) | U6 | IS | 0.88 | 9.88 | 30 | 0.13 | 36.7 | 1.06 | False |
| V7 (current) | U6 | OOS | -2.33 | 11.48 | 31 | 0.24 | 32.3 | 0.86 | False |
| V7 (current) | U6 | FULL | -1.47 | 11.48 | 61 | 0.17 | 34.4 | 0.95 | False |
| C1 48h, 4h trend, MACD+RSI, 4ATR stop, 3R, trail 4ATR@2R, 0.75% | U30 | IS | 3.59 | 12.52 | 147 | 0.66 | 40.1 | 1.05 | False |
| C1 48h, 4h trend, MACD+RSI, 4ATR stop, 3R, trail 4ATR@2R, 0.75% | U30 | OOS | -4.95 | 23.2 | 116 | 0.89 | 33.6 | 0.91 | False |
| C1 48h, 4h trend, MACD+RSI, 4ATR stop, 3R, trail 4ATR@2R, 0.75% | U30 | FULL | -3.85 | 23.2 | 260 | 0.74 | 36.9 | 0.97 | False |
| C1 48h, 4h trend, MACD+RSI, 4ATR stop, 3R, trail 4ATR@2R, 0.75% | U6 | IS | -12.53 | 23.46 | 123 | 0.55 | 33.3 | 0.82 | False |
| C1 48h, 4h trend, MACD+RSI, 4ATR stop, 3R, trail 4ATR@2R, 0.75% | U6 | OOS | -10.53 | 22.41 | 76 | 0.58 | 27.6 | 0.74 | False |
| C1 48h, 4h trend, MACD+RSI, 4ATR stop, 3R, trail 4ATR@2R, 0.75% | U6 | FULL | -13.57 | 28.33 | 197 | 0.56 | 31.5 | 0.87 | False |
| C2 24h, 4h trend, no MACD, RSI80, 4ATR, 3R, trail 4ATR@2R, 0.5% | U30 | IS | 2.01 | 11.09 | 167 | 0.75 | 34.7 | 1.04 | False |
| C2 24h, 4h trend, no MACD, RSI80, 4ATR, 3R, trail 4ATR@2R, 0.5% | U30 | OOS | 1.32 | 13.72 | 133 | 1.02 | 32.3 | 1.03 | False |
| C2 24h, 4h trend, no MACD, RSI80, 4ATR, 3R, trail 4ATR@2R, 0.5% | U30 | FULL | -2.32 | 13.96 | 298 | 0.84 | 32.9 | 0.98 | False |
| C2 24h, 4h trend, no MACD, RSI80, 4ATR, 3R, trail 4ATR@2R, 0.5% | U6 | IS | -16.29 | 16.09 | 147 | 0.66 | 34.7 | 0.74 | False |
| C2 24h, 4h trend, no MACD, RSI80, 4ATR, 3R, trail 4ATR@2R, 0.5% | U6 | OOS | 1.27 | 10.07 | 86 | 0.66 | 34.9 | 1.04 | False |
| C2 24h, 4h trend, no MACD, RSI80, 4ATR, 3R, trail 4ATR@2R, 0.5% | U6 | FULL | -1.61 | 16.09 | 231 | 0.66 | 34.6 | 0.98 | False |
| C3 24h, 1h trend, no MACD/align, 4ATR, 3R, trail 4ATR@2R, 0.5% | U30 | IS | -9.34 | 17.26 | 199 | 0.9 | 30.7 | 0.86 | False |
| C3 24h, 1h trend, no MACD/align, 4ATR, 3R, trail 4ATR@2R, 0.5% | U30 | OOS | 11.12 | 9.58 | 116 | 0.89 | 37.1 | 1.31 | False |
| C3 24h, 1h trend, no MACD/align, 4ATR, 3R, trail 4ATR@2R, 0.5% | U30 | FULL | 0.85 | 17.26 | 317 | 0.9 | 33.1 | 1.01 | False |
| C3 24h, 1h trend, no MACD/align, 4ATR, 3R, trail 4ATR@2R, 0.5% | U6 | IS | -8.76 | 11.7 | 159 | 0.72 | 33.3 | 0.84 | False |
| C3 24h, 1h trend, no MACD/align, 4ATR, 3R, trail 4ATR@2R, 0.5% | U6 | OOS | 1.83 | 11.99 | 102 | 0.78 | 35.3 | 1.06 | False |
| C3 24h, 1h trend, no MACD/align, 4ATR, 3R, trail 4ATR@2R, 0.5% | U6 | FULL | -1.53 | 11.99 | 258 | 0.73 | 34.1 | 0.98 | False |
| C4 48h, 1h trend, MACD, 6ATR, 3R, trail 6ATR@1.5R, 0.75% | U30 | IS | -15.68 | 15.84 | 116 | 0.52 | 33.6 | 0.72 | False |
| C4 48h, 1h trend, MACD, 6ATR, 3R, trail 6ATR@1.5R, 0.75% | U30 | OOS | 9.18 | 9.67 | 82 | 0.63 | 35.4 | 1.26 | False |
| C4 48h, 1h trend, MACD, 6ATR, 3R, trail 6ATR@1.5R, 0.75% | U30 | FULL | -1.73 | 16.31 | 195 | 0.55 | 33.8 | 0.98 | False |
| C4 48h, 1h trend, MACD, 6ATR, 3R, trail 6ATR@1.5R, 0.75% | U6 | IS | -10.96 | 15.52 | 86 | 0.39 | 32.6 | 0.73 | False |
| C4 48h, 1h trend, MACD, 6ATR, 3R, trail 6ATR@1.5R, 0.75% | U6 | OOS | -9.67 | 17.01 | 59 | 0.45 | 27.1 | 0.61 | False |
| C4 48h, 1h trend, MACD, 6ATR, 3R, trail 6ATR@1.5R, 0.75% | U6 | FULL | -14.32 | 18.96 | 143 | 0.41 | 30.8 | 0.75 | False |
| C5 24h, 4h trend, MACD, 6ATR, 3R, trail 6ATR@1.5R, 0.5% | U30 | IS | -10.56 | 8.23 | 99 | 0.45 | 46.5 | 0.75 | False |
| C5 24h, 4h trend, MACD, 6ATR, 3R, trail 6ATR@1.5R, 0.5% | U30 | OOS | 4.69 | 6.56 | 73 | 0.56 | 43.8 | 1.24 | False |
| C5 24h, 4h trend, MACD, 6ATR, 3R, trail 6ATR@1.5R, 0.5% | U30 | FULL | 9.96 | 8.32 | 167 | 0.47 | 44.9 | 1.23 | False |
| C5 24h, 4h trend, MACD, 6ATR, 3R, trail 6ATR@1.5R, 0.5% | U6 | IS | -8.08 | 8.8 | 86 | 0.39 | 36.0 | 0.74 | False |
| C5 24h, 4h trend, MACD, 6ATR, 3R, trail 6ATR@1.5R, 0.5% | U6 | OOS | -7.05 | 12.55 | 62 | 0.48 | 30.6 | 0.63 | False |
| C5 24h, 4h trend, MACD, 6ATR, 3R, trail 6ATR@1.5R, 0.5% | U6 | FULL | -7.55 | 13.81 | 144 | 0.41 | 34.0 | 0.82 | False |
| C6 72h, 4h trend, MACD, 6ATR, 4R, trail 6ATR@1.5R, 0.75% | U30 | IS | -14.52 | 16.74 | 93 | 0.42 | 36.6 | 0.67 | False |
| C6 72h, 4h trend, MACD, 6ATR, 4R, trail 6ATR@1.5R, 0.75% | U30 | OOS | -2.27 | 12.41 | 72 | 0.55 | 38.9 | 0.93 | False |
| C6 72h, 4h trend, MACD, 6ATR, 4R, trail 6ATR@1.5R, 0.75% | U30 | FULL | -12.13 | 19.56 | 162 | 0.46 | 37.0 | 0.82 | False |
| C6 72h, 4h trend, MACD, 6ATR, 4R, trail 6ATR@1.5R, 0.75% | U6 | IS | -11.13 | 15.57 | 77 | 0.35 | 36.4 | 0.71 | False |
| C6 72h, 4h trend, MACD, 6ATR, 4R, trail 6ATR@1.5R, 0.75% | U6 | OOS | -6.94 | 14.52 | 50 | 0.38 | 26.0 | 0.71 | False |
| C6 72h, 4h trend, MACD, 6ATR, 4R, trail 6ATR@1.5R, 0.75% | U6 | FULL | -11.25 | 17.12 | 125 | 0.35 | 32.8 | 0.79 | False |
| C7 12h, 1h trend, no MACD/align, 3ATR, 2R, trail 3ATR@1R, 0.5% | U30 | IS | 9.91 | 14.08 | 487 | 2.19 | 50.7 | 1.09 | False |
| C7 12h, 1h trend, no MACD/align, 3ATR, 2R, trail 3ATR@1R, 0.5% | U30 | OOS | 10.75 | 10.99 | 308 | 2.36 | 47.4 | 1.14 | False |
| C7 12h, 1h trend, no MACD/align, 3ATR, 2R, trail 3ATR@1R, 0.5% | U30 | FULL | 20.57 | 14.08 | 794 | 2.25 | 49.4 | 1.1 | False |
| C7 12h, 1h trend, no MACD/align, 3ATR, 2R, trail 3ATR@1R, 0.5% | U6 | IS | -8.68 | 14.94 | 384 | 1.73 | 47.4 | 0.92 | False |
| C7 12h, 1h trend, no MACD/align, 3ATR, 2R, trail 3ATR@1R, 0.5% | U6 | OOS | 0.71 | 18.54 | 251 | 1.93 | 46.6 | 1.01 | False |
| C7 12h, 1h trend, no MACD/align, 3ATR, 2R, trail 3ATR@1R, 0.5% | U6 | FULL | 6.95 | 18.54 | 635 | 1.8 | 47.4 | 1.04 | False |
| C8 24h, 4h trend, no MACD, RSI80, 3ATR, 2R, trail 3ATR@1R, 0.5% | U30 | IS | -4.9 | 15.84 | 451 | 2.03 | 47.5 | 0.96 | False |
| C8 24h, 4h trend, no MACD, RSI80, 3ATR, 2R, trail 3ATR@1R, 0.5% | U30 | OOS | 19.09 | 12.02 | 278 | 2.13 | 50.4 | 1.27 | False |
| C8 24h, 4h trend, no MACD, RSI80, 3ATR, 2R, trail 3ATR@1R, 0.5% | U30 | FULL | 16.07 | 15.84 | 727 | 2.06 | 48.6 | 1.09 | False |
| C8 24h, 4h trend, no MACD, RSI80, 3ATR, 2R, trail 3ATR@1R, 0.5% | U6 | IS | 0.82 | 8.2 | 287 | 1.29 | 49.8 | 1.01 | False |
| C8 24h, 4h trend, no MACD, RSI80, 3ATR, 2R, trail 3ATR@1R, 0.5% | U6 | OOS | -9.23 | 19.84 | 180 | 1.38 | 42.8 | 0.81 | False |
| C8 24h, 4h trend, no MACD, RSI80, 3ATR, 2R, trail 3ATR@1R, 0.5% | U6 | FULL | 5.27 | 19.84 | 465 | 1.32 | 47.1 | 1.04 | False |

Note: FULL is a separate run, not IS+OOS chained. With the 3-position cap, which signals get a slot depends on
what is already open, so FULL can differ a lot from the halves (e.g. C5 U30: IS −10.56%, OOS +4.69%, FULL +9.96%).
That divergence is itself a sign of noise.

## Chosen: C7 (now the default in `okxbot/config.py`)
12h (12×1h) Donchian breakout; trend filter on 1h (close and EMA50 both on the same side of EMA200);
no MACD, RSI or EMA20/50 filters; stop 3×ATR(14,1h) (min 0.6%, skip if >8%); take-profit 2R; chandelier trail 3×ATR after +1R;
exit if the 1h trend flips against the position; **risk 0.5% of equity per trade**.

| C7 | Return % | Max DD % | Trades | Trades/day | Win % | PF |
|---|---|---|---|---|---|---|
| U30 IS | +9.91 | 14.08 | 487 | 2.19 | 50.7 | 1.09 |
| U30 OOS | +10.75 | 10.99 | 308 | 2.36 | 47.4 | 1.14 |
| **U30 FULL** | **+20.57** | **14.08** | **794** | **2.25** | **49.4** | **1.10** |
| U6 IS | −8.68 | 14.94 | 384 | 1.73 | 47.4 | 0.92 |
| U6 OOS | +0.71 | 18.54 | 251 | 1.93 | 46.6 | 1.01 |
| U6 FULL | +6.95 | 18.54 | 635 | 1.80 | 47.4 | 1.04 |

Why C7: it's the only variant that hit the frequency goal (~2/day across the 30-instrument universe) with max DD under 20%,
and it was positive in both IS and OOS on U30. C8 matched the frequency too but was negative in U30 IS and U6 OOS.

## Robustness check for C7 (U30, full window; one parameter changed at a time)
| Change vs C7 | Return % | Max DD % | Trades | Win % | PF |
|---|---|---|---|---|---|
| C7 base | 20.57 | 14.08 | 794 | 49.4 | 1.1 |
| Donchian 8 | 19.91 | 17.39 | 790 | 48.9 | 1.1 |
| Donchian 16 | 5.06 | 17.51 | 766 | 46.9 | 1.03 |
| stop 2.5ATR | -8.98 | 27.19 | 1119 | 47.1 | 0.97 |
| stop 4ATR | -1.77 | 12.83 | 480 | 45.2 | 0.99 |
| TP 3R | 11.77 | 12.97 | 744 | 47.3 | 1.06 |
| risk 1% | 38.89 | 26.74 | 794 | 49.4 | 1.1 |
| 2x costs | -5.65 | 17.06 | 793 | 46.7 | 0.97 |

## Honest reading of round 2
- **The edge is thin and fragile.** PF is only ~1.1. Moving the stop to 2.5 or 4 ATR, or doubling costs, makes it lose money.
  Real or demo fills that are worse than 0.08%/side would likely wipe out the backtest profit.
- 8 new variants were tried in this round (C1–C8, plus V7 as reference; 8 more in round 1), and C7 was picked after seeing the results. That selection
  bias, plus U30 survivorship bias, means expect worse than the +20.57% shown. On the unbiased U6 pairs, IS was −8.68%.
- Funding is not modelled; with roughly 2 trades/day and short holds it matters less than for V7, but it's not zero.
- Its goal is getting demo experience at a higher trade rate, not proven profitability. With risk at 1% instead of 0.5%,
  the same trades gave +38.89% but 26.74% max DD.


---

# Round 3 (2026-10-10, UTC+8): spot hold-or-USDT rule for held coins (BTC, ETH, OKB)

Mandate: on demo, the bot may sell held coins to USDT and buy them back, to maximize totalEq in USDT terms.
Script: `python3 -m backtests.backtest_spot` (raw: `data/backtest_spot.csv`, `data/backtest_spot.txt`).

Setup: OKX production 1h **spot** candles, last 8760 bars minus 300 warm-up (2025-10-22 → 2026-10-10).
Each coin starts fully held. Signal on a closed 1h bar → trade at the next 1h open. Costs: 0.10% fee + 0.05% slippage per side.
IS = before 2026-06-01, OOS = after. Value is measured in USDT.

**Rule chosen up front ("both", now live):** sell to USDT only when the 4h trend (4h close and EMA20 both below EMA50)
**and** the 1h trend (close and EMA50 both below EMA200) are down. Rebuy only when both are up. Mixed signals mean
keep the current state, and switches are at least 24 h apart (hysteresis). The "4h-only", "1h-only" and "no minimum hold"
variants are shown for comparison only.

| Rule | Min hold h | Coin | Period | Rule return % | B&H return % | Rule max DD % | B&H max DD % | Switches | Time in coin % |
|---|---|---|---|---|---|---|---|---|---|
| both | 24 | BTC-USDT | IS (2025-10-22→2026-05-31) | -19.51 | -32.37 | 28.35 | 45.79 | 29 | 42.0 |
| both | 24 | BTC-USDT | OOS (2026-06-01→2026-10-10) | 15.89 | 12.24 | 9.04 | 21.09 | 13 | 57.0 |
| both | 24 | BTC-USDT | FULL (2025-10-22→2026-10-10) | -6.83 | -24.09 | 28.35 | 49.76 | 41 | 47.6 |
| both | 24 | ETH-USDT | IS (2025-10-22→2026-05-31) | -29.8 | -47.91 | 32.92 | 57.13 | 31 | 38.8 |
| both | 24 | ETH-USDT | OOS (2026-06-01→2026-10-10) | 6.64 | 24.33 | 15.19 | 24.47 | 17 | 66.0 |
| both | 24 | ETH-USDT | FULL (2025-10-22→2026-10-10) | -25.33 | -35.24 | 40.7 | 64.06 | 47 | 48.9 |
| both | 24 | OKB-USDT | IS (2025-10-22→2026-05-31) | -7.56 | -42.32 | 24.96 | 61.09 | 26 | 34.7 |
| both | 24 | OKB-USDT | OOS (2026-06-01→2026-10-10) | 12.1 | 36.21 | 19.67 | 28.07 | 14 | 81.6 |
| both | 24 | OKB-USDT | FULL (2025-10-22→2026-10-10) | 3.62 | -21.44 | 32.04 | 61.12 | 40 | 52.2 |
| both | 0 | BTC-USDT | IS (2025-10-22→2026-05-31) | -18.49 | -32.37 | 27.71 | 45.79 | 29 | 42.1 |
| both | 0 | BTC-USDT | OOS (2026-06-01→2026-10-10) | 15.99 | 12.24 | 8.96 | 21.09 | 13 | 56.8 |
| both | 0 | BTC-USDT | FULL (2025-10-22→2026-10-10) | -5.56 | -24.09 | 27.71 | 49.76 | 41 | 47.6 |
| both | 0 | ETH-USDT | IS (2025-10-22→2026-05-31) | -30.69 | -47.91 | 34.38 | 57.13 | 31 | 39.2 |
| both | 0 | ETH-USDT | OOS (2026-06-01→2026-10-10) | 6.27 | 24.33 | 15.48 | 24.47 | 17 | 67.1 |
| both | 0 | ETH-USDT | FULL (2025-10-22→2026-10-10) | -26.53 | -35.24 | 41.45 | 64.06 | 47 | 49.6 |
| both | 0 | OKB-USDT | IS (2025-10-22→2026-05-31) | -7.3 | -42.32 | 24.96 | 61.09 | 26 | 35.0 |
| both | 0 | OKB-USDT | OOS (2026-06-01→2026-10-10) | 13.82 | 36.21 | 19.67 | 28.07 | 14 | 81.9 |
| both | 0 | OKB-USDT | FULL (2025-10-22→2026-10-10) | 5.51 | -21.44 | 32.04 | 61.12 | 40 | 52.5 |
| 4h | 24 | BTC-USDT | IS (2025-10-22→2026-05-31) | -18.31 | -32.37 | 27.28 | 45.79 | 29 | 42.0 |
| 4h | 24 | BTC-USDT | OOS (2026-06-01→2026-10-10) | 15.89 | 12.24 | 9.04 | 21.09 | 13 | 57.0 |
| 4h | 24 | BTC-USDT | FULL (2025-10-22→2026-10-10) | -5.44 | -24.09 | 27.28 | 49.76 | 41 | 47.6 |
| 4h | 24 | ETH-USDT | IS (2025-10-22→2026-05-31) | -29.43 | -47.91 | 32.57 | 57.13 | 31 | 38.8 |
| 4h | 24 | ETH-USDT | OOS (2026-06-01→2026-10-10) | 6.18 | 24.33 | 15.19 | 24.47 | 17 | 66.2 |
| 4h | 24 | ETH-USDT | FULL (2025-10-22→2026-10-10) | -25.26 | -35.24 | 40.64 | 64.06 | 47 | 49.0 |
| 4h | 24 | OKB-USDT | IS (2025-10-22→2026-05-31) | -7.25 | -42.32 | 24.71 | 61.09 | 26 | 34.8 |
| 4h | 24 | OKB-USDT | OOS (2026-06-01→2026-10-10) | 12.1 | 36.21 | 19.67 | 28.07 | 14 | 81.6 |
| 4h | 24 | OKB-USDT | FULL (2025-10-22→2026-10-10) | 3.96 | -21.44 | 31.81 | 61.12 | 40 | 52.2 |
| 1h | 24 | BTC-USDT | IS (2025-10-22→2026-05-31) | -26.06 | -32.37 | 34.98 | 45.79 | 37 | 41.7 |
| 1h | 24 | BTC-USDT | OOS (2026-06-01→2026-10-10) | 5.34 | 12.24 | 15.64 | 21.09 | 19 | 56.5 |
| 1h | 24 | BTC-USDT | FULL (2025-10-22→2026-10-10) | -22.2 | -24.09 | 37.24 | 49.76 | 55 | 47.2 |
| 1h | 24 | ETH-USDT | IS (2025-10-22→2026-05-31) | -26.85 | -47.91 | 33.98 | 57.13 | 37 | 38.8 |
| 1h | 24 | ETH-USDT | OOS (2026-06-01→2026-10-10) | 13.24 | 24.33 | 13.91 | 24.47 | 19 | 64.3 |
| 1h | 24 | ETH-USDT | FULL (2025-10-22→2026-10-10) | -17.39 | -35.24 | 36.48 | 64.06 | 55 | 48.3 |
| 1h | 24 | OKB-USDT | IS (2025-10-22→2026-05-31) | -11.66 | -42.32 | 26.54 | 61.09 | 36 | 36.1 |
| 1h | 24 | OKB-USDT | OOS (2026-06-01→2026-10-10) | 16.56 | 36.21 | 17.27 | 28.07 | 18 | 80.0 |
| 1h | 24 | OKB-USDT | FULL (2025-10-22→2026-10-10) | 2.96 | -21.44 | 30.69 | 61.12 | 54 | 52.4 |

**Results for the current demo holdings, weighted by value at start (BTC ≈82.6k, OKB ≈12.6k, ETH ≈2.5k USDT), "both"/24h rule:**

| Period | Rule | Buy & hold |
|---|---|---|
| IS | −18.23% | −34.05% |
| OOS | +15.16% | +15.64% |
| FULL | −5.95% | −24.03% |

Honest reading:
- The rule is a **drawdown cutter, not an alpha source.** It beat buy-and-hold by a wide margin in the falling IS period
  (it sat in USDT about 60% of the time). In the rising OOS period it roughly matched B&H for the mix: it was ahead on
  BTC (+15.89 vs +12.24) but well behind on ETH (+6.64 vs +24.33) and OKB (+12.10 vs +36.21), because it re-enters late.
- Max drawdown was roughly halved in every case (e.g. BTC full year 28.35% vs 49.76%).
- 40–47 switches per coin per year, so about 4–5% of the coin value goes to fees and slippage every year.
- "both" and "4h-only" behave almost the same (the 4h filter is the binding one). The 24 h minimum hold barely mattered.
- Spot C7 longs on other pairs reuse the swap strategy's signals (backtested in round 2) and are not backtested separately on spot.


---

# Round 4 (2026-10-10, UTC+8): funding costs in the C7 swap backtest

`python3 -m experiments.experiments_funding` (raw: `data/experiments_funding.csv/.txt`). Funding comes from OKX
`/api/v5/public/funding-rate-history` (settled `realizedRate`). **OKX only serves about 3 months of history**, so real data
starts **2026-06-29 08:00 UTC** for all instruments. Funding is charged at each settlement time to positions opened before it
(longs pay positive rates, shorts pay negative rates), sized at that bar's open price. "FULL" rows fill the period before
2026-06-29 with each instrument's mean rate every 8h. That part is an **assumption**, not data. The "entry filter" skips entries
whose side would pay more than 0.05%/8h at the last settled rate, a proxy for the live filter, which uses the current rate.
FULL "no funding" differs slightly from round 2 (22.40% vs 20.57%) because the candle cache was refreshed through 2026-10-10.

| Universe | Case | Window | Return % | Max DD % | Trades | Trades/day | Win % | PF | Net funding paid (USDT, 500 start) |
|---|---|---|---|---|---|---|---|---|---|
| U30 | REAL-WINDOW no funding | 2026-06-29 08:00 UTC → 2026-10-10 05:00 UTC | 6.46 | 8.15 | 246 | 2.39 | 47.2 | 1.11 | 0.0 |
| U30 | REAL-WINDOW + funding (real) | 2026-06-29 08:00 UTC → 2026-10-10 05:00 UTC | 5.99 | 8.3 | 246 | 2.39 | 47.2 | 1.1 | 2.14 |
| U30 | REAL-WINDOW + funding + entry filter | 2026-06-29 08:00 UTC → 2026-10-10 05:00 UTC | 10.1 | 7.15 | 243 | 2.36 | 49.0 | 1.17 | 1.73 |
| U30 | FULL no funding | 2025-10-22 00:00 UTC → 2026-10-10 05:00 UTC | 22.4 | 12.43 | 793 | 2.25 | 49.6 | 1.11 | 0.0 |
| U30 | FULL + funding (real, mean-filled before) | 2025-10-22 00:00 UTC → 2026-10-10 05:00 UTC | 21.61 | 12.49 | 793 | 2.25 | 49.4 | 1.11 | 3.59 |
| U30 | FULL + funding + entry filter | 2025-10-22 00:00 UTC → 2026-10-10 05:00 UTC | 26.32 | 12.49 | 790 | 2.24 | 50.0 | 1.13 | 3.12 |
| U6 | REAL-WINDOW no funding | 2026-06-29 08:00 UTC → 2026-10-10 05:00 UTC | 0.49 | 12.86 | 201 | 1.95 | 46.3 | 1.01 | 0.0 |
| U6 | REAL-WINDOW + funding (real) | 2026-06-29 08:00 UTC → 2026-10-10 05:00 UTC | 0.35 | 12.78 | 201 | 1.95 | 46.3 | 1.01 | 0.79 |
| U6 | REAL-WINDOW + funding + entry filter | 2026-06-29 08:00 UTC → 2026-10-10 05:00 UTC | 0.35 | 12.78 | 201 | 1.95 | 46.3 | 1.01 | 0.79 |
| U6 | FULL no funding | 2025-10-22 19:00 UTC → 2026-10-10 05:00 UTC | 4.07 | 18.54 | 639 | 1.81 | 46.9 | 1.03 | 0.0 |
| U6 | FULL + funding (real, mean-filled before) | 2025-10-22 19:00 UTC → 2026-10-10 05:00 UTC | 4.22 | 18.44 | 639 | 1.81 | 47.1 | 1.03 | -0.63 |
| U6 | FULL + funding + entry filter | 2025-10-22 19:00 UTC → 2026-10-10 05:00 UTC | 4.22 | 18.44 | 639 | 1.81 | 47.1 | 1.03 | -0.63 |

Honest reading:
- **Funding barely matters for C7:** −0.47 percentage points over the real-data window on U30 (2.14 USDT of funding on a
  500 USDT start) and −0.79 pp over the full year with the mean-fill assumption. On U6 it's about ±0.2 pp. Holds are short
  (hours), and funding on these liquid perps averaged roughly the 0.01%/8h base rate.
- **The entry filter's +4 pp on U30 is not a real edge:** it skipped only 3 of 246 trades, and the difference comes from
  path dependence (freed slots taking other trades). It changed nothing on U6. It stays on because it cheaply prevents
  holding positions that pay extreme funding.

## Round 5: post-event breakout (CPI / NFP / FOMC), 2026-10-10
Files: okxbot/event_dates.py (dates and sources), okxbot/event_fetch.py, backtests/backtest_event.py. Output: data/event_bt_output.txt.
Data: OKX 1m candles for BTC/ETH/SOL-USDT-SWAP, from t-3h to t+12h around each of 106 releases from 2023-06-02 to 2026-10-02
(40 NFP, 39 CPI, 27 FOMC). That's 318 event-symbol samples. Event dates come from the BLS archive PDF names and the Fed FOMC calendar.
Rule: pre-range [t-R, t). From t+W, enter on the first 1m close outside the range within 90 min, optionally requiring
volume >= 2x the pre-range median. Stop is the opposite side of the range or 1.5x ATR15. Exits tested: 2R target with an 8h time stop,
an 8h time stop alone, or a 2x ATR15 trail. Costs: 0.05% fee plus 0.02% slippage per side; if a bar hits both stop and target, I count the stop.
Grid: 48 variants (W 15/30, R 1h/2h, volume filter on/off, 2 stop types, 3 exit types).
* Best full-sample result: W15/R1h/vol2x/range stop/2R, with n=312, win 43%, avgR +0.06, t=+0.75 (not significant).
  In-sample (2023-06 to 2025-03) avgR is +0.13; out-of-sample (2025-04 to 2026-10) it's -0.04.
* Only 8 of 48 variants are positive on the full sample. **0/48 are positive out-of-sample.** The median avgR is -0.06.
  ATR stops are worse than range stops.
* FOMC alone looked good in-sample: 37/48 variants positive, best around +0.38R across 71 trades. Every variant is negative OOS
  (-0.2 to -0.55R). There are only 27 independent events (the 3 coins are highly correlated), and 3 events
  account for 17.3R of the 25.4R total.
* Verdict: no robust edge. Keep the post-event entry block. Don't integrate.

## Round 6: C7 on US stock and commodity perps, 2026-10-10
Script: backtests/bt_tradfi.py. Data: OKX production 1h candles for all available history. The stock perps listed in Feb/Mar 2026 (about 3.1k-5.4k bars each); XAU has 8.7k bars.
17 instruments (MU CRCL NVDA QQQ MRVL PLTR DELL ORCL MSFT HOOD CRWV ASML LLY XAU CL BZ XAG). Portfolio size 10k. Funding is not modelled.
* C7 at 0.5% risk: 2025-10-22 to 2026-10-10: **-20.4%, maxDD 33.3%, 636 trades, win 44.8%, PF 0.88**.
  Before 2026-07: +2.3% (PF 1.02). From 2026-07: -24.1% (PF 0.64).
* Standalone, all 13 stocks are negative (PF 0.63-0.99). Commodities are about flat: XAU +1.9%, CL -0.1%, BZ -2.6%, XAG -6.4%.
  Stocks lose -0.09R per trade over 442 trades; commodities lose -0.01R over 194.
* Entry session makes no difference: cash -0.07R, off-hours -0.07R, weekend -0.04R per trade.
* C7 at 0.25% risk: -10.5% total, maxDD 18.2%. Alternatives tested: 24h/4h-trend/4ATR/3R scored PF 1.11 overall but failed out-of-sample (PF 0.77).
  48h/4h-trend/6ATR/3R scored PF 1.02 overall and 1.08 out-of-sample over 68 trades, which is basically flat.
* Decision (per Wenbo's instruction): add them anyway at reduced risk (0.25% = TRADFI_RISK_MULT 0.5), with a further 0.5x
  on weekends and outside US cash hours for stocks. That x0.5 is precautionary and not supported by the backtest.

## Round 7: stop nudge beyond estimated liquidation clusters, 2026-10-10
Map: okxbot/liqmap.py. It's an estimate built from OKX hourly OI (rubik open-interest-history, which only goes back about 60 days) and 1h candles.
New OI is split 50/50 long/short across leverage tiers 5/10/25/50/100x (weights .1/.3/.3/.2/.1). A level is removed once price trades through it.
Rule: a 0.25% bucket counts as dense if it holds at least 2.5x the average bucket within ±5%. If a dense bucket sits between 0.5 ATR beyond
the stop and 1 ATR toward price, move the stop to the far edge plus 0.25 ATR, as long as the stop stays within MAX_STOP_PCT. Sizing
follows the new stop. The TP price is unchanged.
Test: U30, 2026-08-25 to 2026-10-10 (about 46 days, after a 14-day map warm-up). backtests/bt_liqmap.py, backtests/bt_liqmap_ctrl.py.
| variant | return | maxDD | trades | win | PF |
| C7 baseline | +4.0% | 7.95% | 117 | 47.0% | 1.14 |
| C7 + cluster nudge | +9.5% | 4.52% | 100 | 54.0% | 1.40 |
| control: widen ALL stops +0.89% | +3.3% | 4.91% | 103 | 50.5% | 1.13 |
| control: widen random 51% +0.89% (3 seeds) | +1.1 / +5.6 / +2.8% | 5.8 / 4.6 / 5.7% | 110-111 | 48-51% | 1.04 / 1.21 / 1.10 |
51 of 100 entries were nudged (median +0.89% extra distance). In 12 more, the nudge would have exceeded MAX_STOP, so the original stop was kept.
The nudge beats the blind-widening controls, but this is one 46-day window with about 100 trades and not statistically significant.
Treat it as a weak positive. Realized liquidations (public/liquidation-orders) cover only the last ~100 fills (hours), so the map can't be validated historically.

## Round 8 — time stop ("not working" exit) and MAX_POSITIONS=4 (2026-10-11)
C7, 30-coin universe, funding (real, mean-filled before 2026-06-29) + funding entry filter, same costs.
IS = before 2026-06-01, OOS = after. Script: `experiments/experiments_timestop.py` (backtest.run(time_stop=(N, X, mode))).
Baseline: IS +15.67% (DD 10.96, 479 tr, PF 1.13) | OOS +13.47% (DD 11.17, 308, 1.18) | FULL +29.76% (DD 11.17, 786, 1.15).
Two readings of "hasn't reached +X R after N h": (a) MFE never reached X, (b) unrealized R at N h below X.
Full tables: data/experiments_timestop.csv (MFE + MAXPOS=4), data/experiments_timestop_cur.csv (current-R).
Result: no variant improves both IS and OOS. MFE X=0 never fires (identical to baseline). Short N (24/36 h) hurts badly.
Best IS (current-R N=72) gains IS (+18–19%) but loses OOS (+7.3–9.95% vs +13.47%). MAXPOS=4: IS +22.71% but
OOS +8.09% and DD 21.8% vs 11.2%. Decision: no change. Breakout trades that pay often need >2–3 days.

## Round 9 — spot preference for ~1x longs (2026-10-12), `backtests/bt_spotpref.py`
C7 U30 + funding (filled) + funding filter, 10k start. Routed trades: spot taker 0.1%/side, no funding; swap
candles used as price proxy (spot ≈ perp, basis ignored). 329 of 786 trades are 1x longs.
| | IS ret / DD / PF | OOS ret / DD / PF | FULL ret / DD / PF | funding paid |
|---|---|---|---|---|
| all swap | +15.67 / 10.96 / 1.13 | +13.47 / 11.17 / 1.18 | +29.76 / 11.17 / 1.15 | 62.8 |
| ~1x longs -> spot | +13.23 / 11.40 / 1.11 | +11.80 / 11.55 / 1.16 | +25.14 / 11.55 / 1.12 | 24.8 |
Accounting check: start + Σ trade pnl − final equity is identical in both modes (gap = positions still open at the
window end), so routing doesn't break PnL accounting. Cost: about −4.6 pp/yr, because the spot taker fee
(0.1%) is double the swap taker fee (0.05%), and that outweighs the funding saved (~38 USDT on 10k). Benefits not
modelled: no liquidation/tier caps (avoids 51004), no funding-spike risk. Implemented per user rule; can be
disabled with SPOT_PREFER_FOR_LOW_LEV_LONGS=False.

## Round 10 — funding-harvest exception (2026-10-12), `backtests/bt_harvest.py`
Decision uses the last settled 8h rate at entry (historical predicted rates aren't available). Funding is
real from 2026-06-29; before that it's mean-filled, so the "REAL-FUND window" row is the honest one.
| case | IS | OOS | FULL | real-funding window |
|---|---|---|---|---|
| all swap | +15.67 | +13.47 | +29.76 (DD 11.17) | +9.60 |
| 1x longs -> spot | +13.23 | +11.80 | +25.14 (DD 11.55) | +8.17 |
| -> spot + harvest exception | +13.30 | +11.93 | +25.37 (DD 11.55) | +8.29 |
Harvest trades: 11 over the year (8 in the real-funding window), win rate 18%. Total −275.7 USDT on a 10k start,
of which funding contributed only +7.6. The small gain vs spot-routing comes from the cheaper swap fee, not
from funding. Most strongly negative-funding longs lost on price (crowded shorts tend to be right in
downtrends). Sample is tiny, so this is not evidence of an edge. All-swap is still best overall.
**2026-10-14:** `FUNDING_HARVEST_ENABLED` set to False (spot prefer only); code path kept for possible re-enable.

## Round 11 — liquidity filter / slippage buffer (2026-10-12)
Not backtestable: there is no historical demo order book. Live read-only snapshot (21:0x Beijing, 10k notional
plan, stop 3% away, both sides): 58/70 pass. These fail both sides: MAGIC, ENA, CAP, ZRO, INJ, XPL (stop-side
depth within 1.5% ≈ 2.6k–6k USDT). The buffer shrinks size to ~0.86× (majors, 0.5% floor) / ~0.75× (others, 1%
floor) at a 3% stop. Effect on the backtest engine would be roughly proportional: ~−15–25% of PnL and DD, same
signals. Note that LIT's stop filled ~7.5% beyond the trigger on 2026-10-12 because the order was 10x oversized
vs book depth. At the correct size the depth (~16k within 1.5%) now passes.
