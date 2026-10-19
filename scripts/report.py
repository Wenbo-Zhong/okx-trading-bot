"""Print equity, open positions, today's and cumulative PnL."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import csv, json, os
from okxbot import config
from okxbot.okx_client import OKX, OKXError
from okxbot.bot import load_state, now_local, bot_equity, TRADES_F, EQUITY_F, HALT_F


def main():
    st = load_state()
    x = OKX()
    print(f"=== OKX bot report ({'DEMO account' if x.has_keys else 'DRY-RUN, no API keys'}) "
          f"@ {now_local().strftime('%Y-%m-%d %H:%M')} (UTC+8) ===")
    positions = []
    if x.has_keys:
        try:
            total = float(x.balance()[0].get("totalEq") or 0)
            positions = [p for p in x.positions() if float(p.get("pos") or 0) != 0]
            if config.DEMO and config.DEMO_USE_FULL_EQUITY:
                equity = total
                print(f"(demo: sizing off full account totalEq)")
            else:
                equity = bot_equity(st, positions)
                print(f"(whole account totalEq: {total:,.2f}; bot equity cap {config.LIVE_EQUITY_CAP:.0f})")
        except OKXError as e:
            print("Could not fetch account:", e)
            equity = None
    else:
        equity = st.get("paper_equity", config.START_CAPITAL)
    if x.has_keys and st.get("mode") != "demo":
        st = {}   # dry-run state is not the demo account's state
    start = st.get("start_equity") or equity
    day_start = st.get("day_start_equity") or equity
    if equity is not None:
        print(f"Equity (sizing): {equity:,.2f} USD")
        if st.get("day") == now_local().date().isoformat():
            print(f"Today's PnL:     {equity - day_start:+,.2f} ({(equity / day_start - 1) * 100:+.2f}%) since {day_start:,.2f} at day start")
        else:
            print("Today's PnL:     n/a (bot has not run yet today)")
        print(f"Cumulative PnL:  {equity - start:+,.2f} ({(equity / start - 1) * 100:+.2f}%) vs start {start:,.2f}")
        peak = st.get("peak_equity") or equity
        print(f"Peak equity:     {peak:,.2f}  drawdown {max(0, 1 - equity / peak) * 100:.2f}% (halt at {config.MAX_DRAWDOWN:.0%})")
    if st.get("halted") or os.path.exists(HALT_F):
        print("!!! BOT HALTED:", st.get("halt_reason"))
    if st.get("day_locked"):
        print("!!! Daily loss limit hit: no new trades until tomorrow (UTC+8)")
    print(f"\nOpen positions ({len(positions)}/{config.MAX_POSITIONS}):")
    if not positions:
        print("  none")
    for p in positions:
        rec = st.get("positions", {}).get(p["instId"], {})
        print(f"  {p['instId']:<18} {p.get('posSide'):<5} pos={p['pos']} avgPx={p['avgPx']} lever={p.get('lever')}x "
              f"uPnL={float(p.get('upl') or 0):+.2f} margin={float(p.get('margin') or p.get('imr') or 0):.2f} "
              f"stop={rec.get('stop', '?'):.6g}" if rec else
              f"  {p['instId']} pos={p['pos']} avgPx={p['avgPx']} uPnL={p.get('upl')}")
    if os.path.exists(TRADES_F):
        rows = list(csv.DictReader(open(TRADES_F)))
        closed = [r for r in rows if r["event"] == "closed" and r["pnl"] not in ("", None)]
        pnl = sum(float(r["pnl"]) for r in closed)
        wins = sum(1 for r in closed if float(r["pnl"]) > 0)
        print(f"\nTrade log: {sum(r['event'] == 'open' for r in rows)} entries, {len(closed)} closed, "
              f"realized PnL {pnl:+.2f}, win rate {wins / len(closed) * 100:.0f}%" if closed else
              f"\nTrade log: {sum(r['event'] == 'open' for r in rows)} entries, 0 closed")
        for r in rows[-5:]:
            print("  ", r["time"], r["event"], r["instId"], r["side"], r["price"], r["pnl"], r["reason"])
    else:
        print("\nTrade log: empty")
    try:   # decision review (planner window): by category + C7 / hold / cash baselines; full: scripts/review_report.py
        from scripts.review_report import render
        from okxbot import review as RV
        print("\n" + render(RV.build()))
    except Exception as e:
        print(f"\n(decision review unavailable: {e})")


if __name__ == "__main__":
    main()
