# OKX US-stock exposure: research notes (2026-10-10, Asia/Shanghai)
Status: research only. Nothing in the bot uses these instruments yet.
Raw instrument dump: data/okx_stock_instruments.json (public /api/v5/public/instruments + /market/tickers,
production and demo [x-simulated-trading: 1]). instCategory 3 = stocks/equity products, 4 = commodities.
Note: the demo API also tags some crypto pairs (XRP, AVAX, BCH, SCR) as category 3. That's a demo metadata quirk.

## 1. What OKX lists (production)
* **Stock perpetual swaps (USDT-margined, `<TICKER>-USDT-SWAP`)**: 190 category-3 swaps plus 9 commodity swaps.
  They trade 24/7: Saturday 1h candles show volume. ctVal is 1 share (most). OKX announcements give leverage
  as 0.01-5x, but the instrument API `lever` field reports up to 20-75x (the per-user cap may differ).
  Funding settles every 8h and switches to 1h when funding hits its cap.
  Largest by 24h notional: SNDK ~$403M, SOXL ~$221M, SPCX ~$209M, MU ~$65M, CRCL ~$60M, MSTR ~$40M,
  SKHYNIX ~$38M, NVDA ~$18M, TSLA ~$18M, QQQ ~$18M, AAPL ~$11M, AMD ~$9M, GOOGL ~$7M, SPY ~$3M,
  MSFT ~$3M, AMZN ~$2.8M, META ~$2.1M. Commodities: XAU ~$154M, CL ~$72M, BZ ~$56M, XAG ~$41M.
* **"XPERP" dated-style futures** (`<TICKER>-USD_UM_XPERP-<code>`, ruleType=xperp): 75 stock + 8 commodity,
  e.g. NVDA-USD_UM_XPERP-310613 (~$0.44M/24h). These are thin, and none are on demo.
* **Unified Tokenized Stocks (spot, `X<TICKER>-USDT/USDC`)**: 135 category-3 spot pairs backed 1:1 by xStocks
  (Backed/Payward). Examples: XAAPL-USDT (~$0.58M/24h), XTSLA-USDT (~$0.93M), XNVDA-USDT (~$0.67M),
  XCRCL-USDT (~$11.7M), XSPY-USDT, XQQQ-USDT. Liquidity is far below the perps.
  OKX's spot naming is XAAPL, not the on-chain "AAPLx".

## 2. Demo availability (same endpoints with x-simulated-trading: 1)
* Demo SWAP lists 45 production stock/commodity perps. Demo prices track production on: MU, CRCL, NVDA, QQQ,
  MRVL, PLTR, DELL, ORCL, MSFT, HOOD, CRWV, HIMS, LLY, ASML, OPENAI, ANTHROPIC, NOW, EWJ, HYUNDAI, KIOXIA,
  plus XAU, CL, BZ, XAG.
  Demo prices are broken or stale on: TSLA (199 vs 383 prod), GOOGL (660 vs 353), AAOI, AMD, IWM, CRWD,
  NG, ZHONGJI, SAMSUNG, SPY (~5% off), TSM.
  Not on demo: AAPL, AMZN, META, SNDK, SOXL, SPCX, MSTR, COIN, AVGO, INTC and others.
* Demo spot lists 25 X-tokens (XNVDA, XMSTR, XSPCX, XGOOGL, XMU, XQQQ, XAMD, XTSM ...) plus demo-only
  AAPLX-USDT, TSLAX-USDT, AMZNX-USDT. Most show no demo price or a nonsense one (XNVDA demo 3190 vs 230),
  so they can't be tested on demo.
* No XPERP futures on demo (only demo-only test listings).
* I haven't sent a demo order. Placing one would also require accepting the stock-product risk disclosure in the app,
  which may apply to the demo account as well (not verified).

## 3. Regional restrictions (documented)
* Stock perps: "available to traders across Asia, the CIS region, Latin America, Türkiye, and other eligible
  jurisdictions" (The Block, 2026-03-24). OKX announcements say "check the availability ... for your region in the app".
* Tokenized stocks (UTS/xStocks): not for US persons (Reg S). The terms add "eligible users in select jurisdictions",
  subject to KYC and geographic restrictions.
* OKX's Restricted Locations list includes Hong Kong, Canada, India, Japan, Malaysia, the US (except OKX US) and others.
  Singapore users are served by OKX SG (MAS-licensed DPT: spot only) and OKX Financial (derivatives, "unlicensed
  and unregulated"). The US disclosure also lists Singapore as restricted "regarding our cefi-related services".
  Nothing confirms that stock perps are available to Singapore users.
* Mainland China: OKX's published restricted list doesn't name mainland China. However, the PBoC's 2021 notice
  treats offshore exchanges serving mainland residents as illegal financial activity, and OKX stopped onboarding
  mainland users in 2021 (well-known background, not re-verified today). Eligibility depends on the KYC
  country/residence, not nationality alone.

## Sources
* https://www.okx.com/help/okx-to-list-perpetual-futures-for-selected-stocks (HOOD/TSLA/MSTR, 2026-02-25; 24/7; 0.01-5x)
* https://www.okx.com/help/okx-to-list-perpetual-futures-for-selected-equities (NVDA, MU, SNDK, GOOGL, MSFT, AAPL, META, QQQ, SPY..., 2026-03-04)
* https://www.theblock.co/news/business/2026-03-24-okx-equity-perpetual-swaps-394776
* https://www.okx.com/help/unified-tokenized-stocks, https://www.okx.com/en-us/help/unified-tokenized-stock-trading-terms-and-conditions
* https://www.okx.com/en-sg/help/tokenized-stocks-faq
* https://www.okx.com/help/risk-compliance-disclosure, https://www.okx.com/help/terms-of-service, https://www.okx.com/help/us-risk-and-compliance-disclosures
