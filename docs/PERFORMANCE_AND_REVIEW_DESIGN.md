# 收益分析与复盘子模块设计 V1

## 1. 模块定位

本模块回答：

```text
实际赚了多少？
策略模拟赚了多少？
大盘赚了多少？
实际操作偏离策略多少？
这次投资为什么成功或失败？
```

本模块只消费真实交易、账户估值、策略模拟和基准数据；基准和行情必须按 `DATA_PIPELINE_V1_DESIGN.md` 取得，不重新实现买卖规则。

## 2. 三条必须可比的收益线

### 2.1 实际账户收益

```text
真实现金 + 真实持仓市值
```

### 2.2 策略模拟收益

使用同一：

```text
策略版本
起始资金
日期区间
证券集合
交易成本
```

### 2.3 基准收益

第一版正式基准使用数据平台的 `index_daily/sh000300`。在 `index_daily` 完成发布前，基准比较只能以 `comparison_status=unavailable` 运行，不得用 ETF、个股买入持有或未记录来源的在线数据冒充沪深 300。

三条收益线必须使用相同估值时点，并显示各自数据来源和日期。

## 3. 核心实体

### 3.1 PerformanceQuery

```text
portfolio_id
cycle_id
strategy_version
benchmark_symbol
start_date
end_date
valuation_frequency
```

### 3.2 PerformanceSnapshot

```text
snapshot_id
query_id
as_of
cash
market_value
equity
net_investment
realized_pnl
unrealized_pnl
fees
return_rate
data_context
```

### 3.3 PerformanceComparison

```text
comparison_id
actual_return
simulation_return
benchmark_return
actual_excess_vs_benchmark
actual_gap_vs_simulation
simulation_excess_vs_benchmark
start_date
end_date
assumptions
```

### 3.4 PositionCycleReview

```text
review_id
cycle_id
discovery_reason
research_summary
simulation_run_id
planned_entry
actual_entry
planned_exit
actual_exit
advice_summary
execution_deviation
result_summary
lessons
status
```

### 3.5 ReviewEvidence

```text
evidence_id
review_id
evidence_type
source_type
source_id
summary
snapshot
created_at
```

`source_type`：

```text
screen_run
research_run
simulation_run
strategy_decision
advice
execution
position_snapshot
benchmark
```

状态：

```text
pending
in_progress
completed
archived
```

## 4. 收益口径

### 4.0 逐日权益曲线重算

对每个交易日 `T`：

```text
读取 T 日之前已生效的 Execution
→ 应用 T 日之前的公司行为
→ 计算 T 日 Cash Balance
→ 聚合剩余 PositionLot 数量
→ 使用 T 日收盘价估值
→ 得到 T 日 Equity
```

```text
equity(T) = cash_balance(T) + Σ quantity_i(T) × close_price_i(T)
```

每个点至少保存：

```text
date
cash
market_value
equity
external_cash_flow
realized_pnl
unrealized_pnl
fees
dividend_income
data_context
```

### 4.1 账户收益

```text
net_pnl = ending_equity - beginning_equity - net_external_cash_flow
return_rate = net_pnl / invested_capital
```

第一版必须明确外部现金注入和取出，不能把追加本金误计为投资收益。

### 4.2 交易收益

交易收益由 PositionCycle 和 FIFO Lot 核算服务产生：

```text
realized_pnl
unrealized_pnl
dividend_income
fees
tax
net_pnl
```

### 4.3 自选收益

自选股票的收益只能命名为“观察期假设收益”，不能与真实账户收益混用。它必须显示：

```text
observation_start
assumed_entry_price
assumed_quantity / amount
data_as_of
```

### 4.4 基准数据契约

第一版默认：

```text
benchmark_dataset = index_daily
benchmark_symbol = sh000300
benchmark_price_field = close
```

基准缺失时，实际收益仍可计算，`benchmark_return` 和 `excess_return` 为空，比较状态为 `unavailable`，整体查询标记为 `partial`。禁止临时调用未记录来源的在线基准。

## 5. 指标

第一版至少提供：

```text
total_return
annualized_return
max_drawdown
win_rate
profit_factor
average_profit
average_loss
trade_count
average_holding_days
fees
tax
slippage
benchmark_return
excess_return
```

指标计算必须基于交易和权益曲线，不能只根据当前 Position 字段推断历史结果。

## 6. 复盘链路

```text
DiscoveryCandidate
→ Observation
→ ResearchRun
→ SimulationRun
→ EntryPlan
→ Actual Execution
→ Advice History
→ Exit Execution
→ PerformanceSnapshot
→ PositionCycleReview
```

复盘页面至少展示：

1. 发现来源和筛选条件。
2. 研究时的策略版本和数据日期。
3. 模拟结果和模拟假设。
4. 计划买入价、实际买入价和偏差。
5. 计划仓位、实际仓位和偏差。
6. 持仓期间产生的建议。
7. 用户是否执行建议。
8. 卖出原因和实际结果。
9. 实际、模拟和基准收益对比。
10. 人工总结和后续改进。

## 7. 执行偏差

建议至少记录：

```text
entry_price_deviation
entry_time_deviation
quantity_deviation
exit_price_deviation
exit_time_deviation
advice_follow_rate
```

偏差不能直接判定为错误，页面需要区分：

```text
策略没有给出建议
用户未执行建议
用户部分执行
用户主动偏离
数据不可得
```

## 8. API 目标

```text
GET  /api/performance/portfolio/{portfolio_id}
GET  /api/performance/cycles/{cycle_id}
GET  /api/performance/comparison
GET  /api/performance/equity-curve
POST /api/reviews/cycles/{cycle_id}
PATCH /api/reviews/{review_id}
GET  /api/reviews/{review_id}
GET  /api/reviews/{review_id}/evidence
GET  /api/performance/export
```

接口响应必须带：

```text
data_as_of
valuation_time
source
strategy_version
simulation_run_id
benchmark_symbol
assumptions
```

## 9. 页面设计

### 9.1 账户总览

展示：

- 总资产；
- 可用现金；
- 持仓市值；
- 已实现收益；
- 未实现收益；
- 总收益率；
- 基准收益；
- 策略模拟收益；
- 估值日期。

### 9.2 持仓周期详情

展示：

- 交易时间线；
- 状态变化；
- 建仓计划；
- 建议记录；
- 实际成交；
- 收益曲线；
- 模拟和基准对比。

### 9.3 复盘编辑

支持用户填写：

- 选择原因；
- 当时判断；
- 执行问题；
- 结果评价；
- 后续调整。

## 10. 当前能力映射

| 当前能力 | 目标归属 |
|---|---|
| 新 `PerformanceService` | Performance service |
| `portfolio/trade_metrics.py` | Metrics calculator |
| `portfolio/dashboard.py` | Performance read model |
| `review.html` | Review UI |
| 新 `PerformanceExportService` | Export service |
| `transactions` | Execution source |
| `SimulationResult` | Simulation comparison source |

## 11. 实施步骤

1. 统一 FIFO、费用、分红和现金流口径。
2. 实现账户权益曲线服务。
3. 接入策略模拟结果和基准曲线。
4. 实现实际/模拟/基准对比。
5. 增加 PositionCycleReview 和证据关联。
6. 迁移复盘页面，停止直接拼装多个收益口径。
7. 增加导出字段：收益日期、数据源、版本、失败项和假设。

## 12. 测试与验收

1. 追加现金不计为收益。
2. FIFO 交易结果与持仓收益一致。
3. 手续费、税费和分红正确进入收益。
4. 已平仓周期仍出现在历史收益中。
5. 实际、模拟和基准使用相同日期区间。
6. 缺少行情时结果明确标记，不静默删除证券。
7. 复盘能追溯到 Discovery、Observation、Simulation 和 Execution。
8. 同一交易周期重复计算结果一致。
9. 导出包含数据日期和来源。
