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

详细设计见 [docs/DETAILS.md](docs/DETAILS.md)，回测记录见 [docs/BACKTEST.md](docs/BACKTEST.md)。

## 目录结构

```
okxbot/        核心代码包：bot、strategy、risk、sizing、spot、spot_alloc、news、events、
               event_dates、event_fetch、funding、tradfi、liqmap、alerts、journal、
               journal_log、indicators、okx_client、config
scripts/       入口脚本：run_once.py（跑一轮决策）、report.py（报表）
backtests/     回测脚本：backtest*.py、bt_*.py
experiments/   已完成的实验脚本：experiments*.py
tests/         测试，直接用 python3 运行
docs/          BACKTEST.md、DETAILS.md、US_STOCKS_OKX.md
data/          运行数据（不入库）
```

所有命令都在项目根目录下用 `python3 -m 包名.脚本名` 运行，例如 `python3 -m backtests.backtest`。

## 安装与运行

```bash
python3 -m pip install requests pandas numpy   # 按 import 报错补装
cp .env.example .env    # 填入 OKX 模拟盘 API key（只给读取+交易权限，绝不开提币）
set -a; . ./.env; set +a
python3 -m scripts.run_once     # 跑一轮决策；没有 key 时只做 dry-run
python3 -m scripts.report       # 查看权益 / 持仓 / 盈亏
```

定时运行可用 cron，例如 `8,38 * * * *`。运行数据写在 `data/`（已被 .gitignore 排除）。

现货趋势单在发送前保存订单意图；成交查询失败或进程重启后，下一轮按 `ordId` / `clOrdId`
恢复成交和持仓，不重复提交。订单状态无法确认、止损数量不足且修复未确认、或持仓数量对账有差异时，
暂停新开仓并输出错误。已有持仓管理先于全市场行情加载，行情列表故障不会跳过现货熔断清仓。

部分平仓按实际成交扣减数量，保留并保护剩余持仓；日志汇总各次退出的盈亏、费用和加权成交价。
旧持仓缺少止损订单 ID 时，余额差异会保留为待对账数量，不用当前价格估算为整笔已平仓。

## Planner snapshot（只读信息快照）

```bash
python3 -m scripts.snapshot
python3 -m scripts.snapshot --inst BTC-USDT-SWAP ETH-USDT --no-news --view
python3 -m scripts.snapshot --out /tmp/okx-snapshot.json
```

快照供外部 planner 使用，C7 仅作为参考，不触发交易或修改机器人状态，也不计算新闻情绪分数。
默认输出 JSON；`--view` 输出紧凑文本及 snapshot ID。每次同时原子写入
`<DATA_DIR>/snapshots/<snapshot_id>.json` 和 `latest.json`，`--out` 另存一份 JSON。
缺少 API keys 时 `account=null`；部分数据获取失败仍返回退出码 0，详情见 `errors` 和
`completeness`。`--no-news` 会将 `completeness.news` 设为 false。

K 线仅含已收盘数据，指标使用额外历史预热；历史不足的指标为 null。收益率、资金费率和手续费
使用小数比例；手续费正值表示费用，负值表示返佣，`fees.source` 标记账户费率或配置默认值。
行情和盘口来自生产公共接口，账户数据来自模拟盘。`liquidity_ok` 使用现有流动性过滤器，
以 `START_CAPITAL * MAX_MARGIN_PCT` 名义金额、`MIN_STOP_PCT` 止损距离同时检查两个方向，
仅供参考；具体计划仍需检查模拟盘执行盘口。清算簇是已有 liqmap 模型的估算。
宏观事件来自 `DATA_DIR/events.json`，缺少日历会显式标记不完整。

SHA-256 覆盖去除 `snapshot_id`、`sha256` 后的 UTF-8 JSON，使用排序键、
`ensure_ascii=False`、紧凑分隔符 `(',', ':')`，不允许 NaN。

## 规划模式（2026-10-14 起）

交易决策由外部 LLM 规划器（定时任务里的 Grok Bot）根据新闻和 K 线做出；本项目只负责整理信息、校验并安全执行明确的计划、以及自动安全维护。关键词新闻情绪分不再参与决策；C7 只作为快照里的参考信号和回测基准，所有 `AUTO_*` 开关默认关闭。

```bash
python3 -m scripts.maintain                 # 自动维护：对账、订单恢复、熔断、止损覆盖（run_once 等同于它）
python3 -m scripts.snapshot --brief         # 信息快照 JSON（--view 紧凑文本，--inst 指定品种）
python3 -m scripts.execute_plan plan.json   # 校验计划，默认 dry-run 不下单
python3 -m scripts.execute_plan plan.json --execute   # 执行（模拟盘）
python3 -m scripts.execute_plan --no-trade --snapshot-id ID --reason "信息不足"
python3 -m scripts.decisions                # 查看决策记录及结果
python3 -m scripts.review_report            # 复盘：按类别统计净盈亏，对比 C7 / 持有 / 现金基线
```

计划过期、快照超过 45 分钟或信息不足时不交易。快照字段、计划格式和校验规则见 [docs/PLANNER.md](docs/PLANNER.md)。

## 测试

```bash
.venv/bin/python tests/run_all.py
```

## English summary

Planner mode: an external LLM decides trades; this repo provides `scripts.snapshot` (info), `scripts.execute_plan` (validate, dry-run default, `--execute`), `scripts.maintain` (automatic safety only) and `scripts.decisions` (journal). C7 is reference/backtest only. See docs/PLANNER.md.


Trend-following bot for OKX v5 (strategy "C7"), demo mode by default. Guardrails: ≤5x leverage, ≤3 swaps, mandatory stop-loss, 0.5% risk/trade, −10% daily lock, −40% drawdown halt, spot-preferred longs, balance/exchange-limit-aware sizing, thin-book filter and slippage buffer. Backtests are modest and the live demo has been **losing money**. Configure credentials via `.env` (see `.env.example`), run `python3 -m scripts.run_once`, run `tests/test_*.py` directly.

## License

Copyright (C) 2026 Wenbo Zhong

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License v3.0 (see [LICENSE](LICENSE)). It is distributed WITHOUT ANY WARRANTY.

## Event monitor (`scripts.monitor`)

Watches a configurable watchlist (majors on 15m/1H, ~15 weekly-rotated alts, US stock tokens during US
cash hours, commodities, plus held positions) over the OKX market WebSocket, with REST backfill on reconnect,
low-frequency news polling and hysteresis. Events are written to `data/monitor_events.jsonl`; wakes are
coalesced and capped (1/hour, 8/day; severe faults bypass). Log-only by default.

    python3 -m scripts.monitor --watchlist [--force]   # dry run: print watchlist
    python3 -m scripts.monitor                          # run, log-only
    OKX_WAKE_WEBHOOK_URL=... python3 -m scripts.monitor --enable-wakes

Thresholds live in `okxbot/monitor/settings.py` (override via `data/monitor_config.json`).
