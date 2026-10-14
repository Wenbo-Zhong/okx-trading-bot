# OKX 趋势交易机器人（模拟盘）/ OKX v5 Demo Trend Bot

> ⚠️ **免责声明 / Disclaimer**
> 本项目**仅供学习和研究**，**不构成任何投资建议**。加密货币和合约交易风险极高，可能损失全部本金。使用本代码产生的任何亏损由使用者自行承担，作者不承担任何责任。默认只连 OKX **模拟盘**（`DEMO = True`）。
> For **learning/research only. Not investment advice.** Use at your own risk; the author is **not liable for any losses**. Runs on the OKX **demo** account by default.

## 这是什么

基于 OKX v5 API 的趋势跟随机器人，策略代号 **C7**（4h/1h 趋势 + 回调入场），每 30 分钟跑一轮。内置风控护栏：

- 最大杠杆 5x，最多 3 个合约仓位，每笔必带止损，单笔风险 0.5% 权益
- 单日亏损 −10% 锁仓当天，距峰值回撤 −40% 熔断
- 能买现货时优先现货，不开 1 倍永续多单
- 按可用余额、交易所 `max-size` 和单笔上限下单，避免下单失败
- 盘口过薄（±1.5% 深度不足以覆盖订单）不交易；按滑点（主流币 0.5%、其他 1%）缩小仓位
- 清算密集区止损避让（亏损 ≤ 计划的 1.5 倍）、高影响新闻暂停开多

**成绩如实说明：** 回测收益不高（约 +8%～+26%，盈亏比约 1.1），模拟盘实跑目前是**亏损**的（约 −10%，含一次合约面值 bug）。请不要把它当成赚钱工具。

详细设计见 [docs/DETAILS.md](docs/DETAILS.md)，回测记录见 [BACKTEST.md](BACKTEST.md)。

## 安装与运行

```bash
python3 -m pip install requests pandas numpy   # 按 import 报错补装
cp .env.example .env    # 填入 OKX 模拟盘 API key（只给读取+交易权限，绝不开提币）
set -a; . ./.env; set +a
python3 run_once.py     # 跑一轮决策；没有 key 时只做 dry-run
python3 report.py       # 查看权益 / 持仓 / 盈亏
```

定时运行可用 cron，例如 `8,38 * * * *`。运行数据写在 `data/`（已被 .gitignore 排除）。

## 测试

```bash
for t in tests/test_*.py; do python3 "$t"; done
```

## English summary

Trend-following bot for OKX v5 (strategy "C7"), demo mode by default. Guardrails: ≤5x leverage, ≤3 swaps, mandatory stop-loss, 0.5% risk/trade, −10% daily lock, −40% drawdown halt, spot-preferred longs, balance/exchange-limit-aware sizing, thin-book filter and slippage buffer. Backtests are modest and the live demo has been **losing money**. Configure credentials via `.env` (see `.env.example`), run `python3 run_once.py`, run `tests/test_*.py` directly.

## License

Copyright (C) 2026 Wenbo Zhong

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License v3.0 (see [LICENSE](LICENSE)). It is distributed WITHOUT ANY WARRANTY.
