"""Central configuration. Guardrails here are hard limits enforced in code."""
import os

BASE_URL = "https://www.okx.com"
DEMO = True  # simulated trading only; sends header x-simulated-trading: 1

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TIMEZONE = "Asia/Shanghai"      # day boundary for the daily loss limit

# ---- Guardrails (hard limits) ----
MAX_LEVERAGE = 5                 # never exceed 5x
MAX_MARGIN_PCT = 0.20            # max 20% of equity as margin per position
MAX_POSITIONS = 3                # max concurrent positions
DAILY_LOSS_LIMIT = 0.10          # -10% vs day-start equity -> no new trades today, flatten
MAX_DRAWDOWN = 0.40              # -40% from peak equity -> halt + flag (manual reset)
UNIVERSE_SIZE = 30               # only top-N crypto USDT perps by 24h USDT volume
EXCLUDE_BASE = {"USDC", "USDE", "DAI", "FDUSD", "TUSD", "PYUSD", "USDG", "RLUSD", "XAUT", "PAXG"}  # stablecoins / gold tokens
START_CAPITAL = 500.0            # dry-run default; also the planned live-trading hard cap later
DEMO_USE_FULL_EQUITY = True      # on demo, size off live totalEq (whole demo account); later set False + LIVE_CAP
LIVE_EQUITY_CAP = 500.0          # when DEMO_USE_FULL_EQUITY is False, equity for sizing is min(totalEq, this)


# ---- Strategy parameters ---- (C7, chosen 2026-10-09; previous default V7: 168h/8ATR/4R/1%/4h trend+MACD+RSI75)
RISK_PER_TRADE = 0.005            # equity fraction lost if stop is hit (C7)
DONCHIAN_N = 12                  # breakout lookback in 1h bars (C7)
ATR_STOP_MULT = 3.0              # stop distance = 3 * ATR(14, 1h)
TP_R_MULT = 2.0                  # take profit at 2R
BREAKEVEN_AT_R = 99              # breakeven move disabled (hurt in backtest)
TRAIL_ATR_MULT = 3.0             # chandelier trail distance (ATR multiples)
TRAIL_START_R = 1.0              # trail activates after +1R
TREND_TF = "1h"                  # "4h" (EMA20/50 on 4h) or "1h" (EMA50/200 on 1h)
USE_MACD = False                 # require MACD histogram in trade direction
RSI_CAP = 100                     # long needs RSI < cap, short RSI > 100-cap (100 = off)
REQUIRE_EMA_ALIGN = False        # require 1h EMA20 vs EMA50 alignment
MIN_STOP_PCT = 0.006             # stops tighter than this are widened
MAX_STOP_PCT = 0.08              # signals needing wider stops are skipped
TD_MODE = "isolated"             # isolated margin per position
NEWS_BLOCK_THRESHOLD = 0.35      # |sentiment| beyond this blocks counter-trend side

# ---- Spot allocation (held coins) + spot longs (DEMO mandate 2026-10-10) ----
SPOT_ALLOC_ENABLED = True
SPOT_ALLOC_COINS = ["BTC", "ETH", "OKB"]   # coins managed as hold-or-USDT sleeves
SPOT_ALLOC_RULE = "both"         # "both" = 4h AND 1h trend must agree to switch (hysteresis)
SPOT_MIN_SWITCH_H = 24           # min hours between switches per coin (anti-churn)
SPOT_MIN_ORDER_USDT = 10.0       # skip dust trades below this value
SPOT_LONGS_ENABLED = True        # C7 long signals on liquid spot pairs (no leverage/borrowing)
SPOT_MAX_LONGS = 2
SPOT_LONG_UNIVERSE = 20          # top-N spot USDT pairs by volume
SPOT_RISK_PER_TRADE = 0.005      # same risk as swaps; notional also <= 20% equity
SPOT_FEE = 0.001                 # OKX spot taker 0.1% (used in backtest)

# ---- Funding / event / news risk (2026-10-10) ----
FUNDING_FILTER = True
FUNDING_MAX_PAY_8H = 0.0005      # skip new swap entries that would PAY > 0.05% per 8h (8h-equivalent)
EVENT_BLOCK_BEFORE_H = 2.0       # block new entries from 2h before a high-impact macro event ...
EVENT_BLOCK_AFTER_H = 0.5        # ... until 30 min after it
EVENT_HALVE_BEFORE_H = 6.0       # halve new position size 2-6h before an event
NEWS_HIGH_IMPACT_HOURS = 6       # high-impact negative headline in last N hours -> block new longs
ALERT_COOLDOWN_H = 4             # don't repeat the same alert within N hours

# ---- US stock / commodity perps ("TradFi", approved 2026-10-10 15:14) ----
TRADFI_ENABLED = True
TRADFI_LIST = ["QQQ", "SPY", "XAU", "CL", "BZ", "XAG"]  # user 10/10: stocks only Nasdaq (QQQ) + S&P (SPY); SPY auto-skipped while demo price is off
TRADFI_COMMODITIES = {"XAU", "CL", "BZ", "XAG"}
TRADFI_MAX_MARK_DEV = 0.02       # skip if demo mark deviates > 2% from production mark (checked every cycle)
TRADFI_MIN_VOL_USDT = 2_000_000  # min production 24h notional
TRADFI_RISK_MULT = 0.5           # C7 backtest on these was negative (PF 0.88) -> 0.25% risk instead of 0.5%
TRADFI_OFFSESSION_MULT = 0.5     # further x0.5 outside US cash session (stocks) / on weekends (all), gap risk
# Shares the MAX_POSITIONS swap cap (no separate slots).

# ---- Estimated liquidation clusters (reference only; liqmap.py, approved 2026-10-10) ----
LIQMAP_STOP_NUDGE = True         # nudge new swap stops beyond a dense estimated liq cluster (<= MAX_STOP_PCT)
LIQMAP_ALERTS = True             # alert on dense clusters within LIQMAP_ALERT_SPAN of price (majors + held swaps)
LIQMAP_ALERT_SPAN = 0.02
LIQMAP_NUDGE_OPEN = True         # also nudge stops of OPEN swaps / trailing stops (approved 2026-10-10 15:34)
LIQMAP_MAX_RISK_MULT = 1.5       # nudged stop may not raise max loss above 1.5x the original planned risk
SL_TRIGGER_BUFFER_PCT = 0.001  # SL must stay this far beyond live last AND mark (OKX rejects marketable triggers)

# Endpoints the bot is allowed to call with keys (whitelist). No withdrawal / transfer.
PRIVATE_WHITELIST = {
    ("GET", "/api/v5/account/balance"),
    ("GET", "/api/v5/account/config"),
    ("GET", "/api/v5/account/positions"),
    ("GET", "/api/v5/account/positions-history"),
    ("POST", "/api/v5/account/set-account-level"),
    ("POST", "/api/v5/account/set-position-mode"),
    ("POST", "/api/v5/account/set-leverage"),
    ("POST", "/api/v5/trade/order"),
    ("POST", "/api/v5/trade/cancel-order"),
    ("POST", "/api/v5/trade/close-position"),
    ("GET", "/api/v5/trade/order"),
    ("GET", "/api/v5/trade/orders-algo-pending"),
    ("POST", "/api/v5/trade/order-algo"),
    ("POST", "/api/v5/trade/amend-algos"),
    ("POST", "/api/v5/trade/cancel-algos"),
    ("GET", "/api/v5/trade/fills"),
    ("GET", "/api/v5/account/max-size"),         # read-only: affordable size incl. tier caps
    ("GET", "/api/v5/account/max-avail-size"),   # read-only
    ("GET", "/api/v5/account/bills"),            # read-only: funding fee bills (journal funding per trade)
}
# Route effectively-unlevered long signals to the spot pair (no funding, no liquidation) when one exists.
SPOT_PREFER_FOR_LOW_LEV_LONGS = True
SPOT_PREFER_MAX_LEV = 1          # planned swap leverage <= this counts as "effectively 1x"
# Funding-harvest exception (DISABLED 2026-10-14): when True, a ~1x long whose funding is meaningfully
# negative (longs RECEIVE) stays a 1x swap (intent="funding_harvest") instead of going to spot.
# Threshold: receive >= 0.02% per 8h (~22%/yr). Kept off — backtest (bt_harvest) showed ~11 trades/yr,
# net loss on price; live demo never took one. Spot preference (SPOT_PREFER_*) stays on.
FUNDING_HARVEST_ENABLED = False
FUNDING_HARVEST_MIN_8H = 0.0002   # current 8h-equivalent rate must be <= -0.02% (and last settled rate <= 0)
FUNDING_HARVEST_FLIP_8H = 0.0001  # while held: rate >= +0.01%/8h counts as "flipped positive" (longs pay)
FUNDING_HARVEST_FLIP_CYCLES = 6   # flagged after this many consecutive cycles (~3h at 30-min cadence)
FUNDING_HARVEST_CLOSE_8H = 0.0005 # close only if persistently flipped AND rate >= the entry-block level (0.05%/8h);
                                  # otherwise just flag. Stops/TP/trail are never removed.
ORDER_SIZE_BUFFER = 0.95
ORDER_SANITY_TOL = 1.2
# Liquidity filter + stop-slippage buffer from the DEMO order book (orders execute there).
LIQ_FILTER_ENABLED = True
LIQ_DEPTH_PCT = 0.015            # depth counted within 1.5% of mid
LIQ_MIN_COVER = 1.0              # entry-side AND stop-side depth must be >= 1.0x the (shrunk) order notional
SLIP_FLOOR_MAJOR = 0.005         # min assumed stop slippage, majors (BTC/ETH/SOL/XRP/DOGE/BNB...)
SLIP_FLOOR_OTHER = 0.01          # min assumed stop slippage, everything else (book impact used if larger)
SLIP_MAJORS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "LTC", "BCH", "LINK", "AVAX", "XAU"]         # refuse a swap order if notional or risk > 1.2x the risk-based plan
SPOT_DEPTH_FALLBACK_FRAC = 0.5  # routed ~1x long: if depth cap < 50% of planned size, open the 1x swap instead
SPOT_DEPTH_MAX_SLIP = 0.01       # spot market buys: cap notional to ask depth within 1% of mid (demo book) x 0.8         # use at most 95% of affordable / exchange max size
