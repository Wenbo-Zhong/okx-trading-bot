"""All monitor thresholds. Edit here (or override via data/monitor_config.json, same keys)."""
import json, os
from okxbot import config

DEFAULTS = {
    # --- watchlist ---
    "majors": ["BTC", "ETH", "SOL", "BNB", "OKB"],
    "major_bars": ["15m", "1H"],
    "alt_count": 15,
    "alt_top_n_volume": 40,          # pool = top-N USDT perps by 24h USDT volume
    "alt_max_spread_pct": 0.05,      # best bid/ask spread, percent of mid
    "alt_vol_min_pct": 3.0,          # 7-day stdev of daily returns, percent
    "alt_vol_max_pct": 12.0,
    "alt_min_top_depth_usdt": 20000, # chronically thin: depth within band, each side, below this
    "alt_depth_band_pct": 0.3,       # depth band around mid, percent
    "alt_bars": ["1H"],
    "alt_rotation_weekday": 0,       # Monday (Asia/Shanghai)
    "alt_exclude_always": ["ZRO"],   # known chronically thin books
    "user_exclude": [],              # user's personal exclude list
    "stock_core": ["QQQ", "SPY"],
    "stock_candidates": ["NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "COIN", "MSTR"],
    "stock_extra": 2,                # add the N highest-volume candidates
    "stock_bars": ["1H"],
    "commodities": ["XAU", "XAG", "CL", "BZ"],
    "commodity_bars": ["1H"],
    "held_bars": ["15m", "1H"],
    # --- signals (closed bars) ---
    "history_bars": 300,
    "ema_fast": 20, "ema_slow": 50,
    "breakout_lookback": 24,
    "atr_n": 14,
    "anomaly_body_atr": 1.8,         # |close-open| >= k*ATR
    "anomaly_rel_volume": 3.0,       # volume >= k * median volume of last 48 bars
    "anomaly_hv_z": 2.5,             # |log return| >= k * historical vol (stdev of last 72 log returns)
    "rearm_body_atr": 0.8,           # anomaly re-arms once a bar drops below these
    "rearm_rel_volume": 1.5,
    "breakout_rearm_atr": 0.5,       # breakout re-arms after close returns inside channel by this much ATR
    # 15m trend_change must clear both to be wake-worthy; weaker flips are logged as priority=low
    "trend_change_15m_min_rel_vol": 1.0,
    "trend_change_15m_min_body_atr": 0.5,
    # --- position vs plan ---
    "stop_proximity_atr": 0.3,       # price within k*ATR(1H) of stop -> deviation
    "stop_proximity_rearm_atr": 0.8,
    "adverse_r": 0.7,                # unrealised loss >= 0.7R -> deviation
    "adverse_rearm_r": 0.4,
    "stop_breach_grace_s": 120,      # price beyond stop this long with position still open -> severe fault
    # --- news ---
    "news_poll_s": 600,
    "news_max_age_h": 3,
    # --- wakes ---
    "coalesce_s": 90,
    "wake_min_interval_s": 3600,
    "wake_max_per_day": 8,
    "fault_cooldown_s": 900,
    # same kind+inst+bar cannot wake again within this window unless direction flips with high rel_vol
    "kind_cooldown_s": 7200,
    "kind_cooldown_flip_rel_vol": 1.5,
    # --- transport / rate limits ---
    # port 443 (8443 is unreachable from the box)
    "ws_public": "wss://ws.okx.com/ws/v5/public",
    "ws_business": "wss://ws.okx.com/ws/v5/business",
    "ws_new_conn_per_s": 3,
    "ws_sub_ops_per_hour": 480,
    "ws_args_per_op": 100,
    "rest_min_gap_s": 0.12,
    "state_poll_s": 30,
}

PATH = os.path.join(config.DATA_DIR, "monitor_config.json")


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg
