# 账户、持仓与交易子模块设计 V1

## 1. 模块定位

本模块记录用户真实发生的投资操作，并计算账户现金、当前持仓、资产和持仓阶段。

核心链路：

```text
Observation / StrategyDecision
→ 建仓上下文
→ 用户录入实际成交
→ PositionCycle
→ Execution
→ CashLedger
→ PositionSnapshot
→ Advice / Performance
```

第一版是手工记账系统，不接券商自动交易，不实现订单撮合。

本模块不负责：

- 重新计算选股条件；
- 重新实现策略规则；
- 直接发送通知；
- 用页面字段替代交易事实。

## 2. 产品范围

### 必须支持

1. 账户和组合。
2. 初始现金和现金流水。
3. 真实买入、卖出、分红、费用和纠错。
4. 建仓、加仓、减仓和清仓。
5. 持仓当前数量、成本、价格、市值和收益。
6. 一轮建仓到清仓的 `PositionCycle`。
7. 真实成交与策略、观察、建议和模拟的关联。
8. 持仓阶段和状态变化记录。
9. 资产总览和持仓明细。
10. 手工交易的幂等保护和审计。

### 暂不支持

- 券商 API；
- 委托、撤单和成交回报同步；
- 多用户共享账户；
- 复杂税务核算；
- 自动根据建议下单。

## 3. 核心实体

### 3.1 Account

```text
account_id
name
currency
account_type
status
created_at
```

第一版 `account_type`：

```text
real
paper
```

### 3.2 Portfolio

```text
portfolio_id
account_id
name
benchmark_symbol
status
created_at
updated_at
```

所有余额和持仓查询必须显式带 `portfolio_id`，禁止默认混用组合 1。

### 3.3 PositionCycle

表示一次从建仓到清仓的投资周期。

```text
cycle_id
portfolio_id
symbol
strategy_id
strategy_version
observation_id
simulation_run_id
status
phase
opened_at
closed_at
 entry_plan_snapshot
scheme_snapshot
created_at
updated_at
```

`status`：

```text
planned
open
closed
cancelled
```

`phase`：

```text
accumulating
holding
left_take_profit
right_trailing
stopped
closed
```

### 3.4 PositionLot

记录每次买入批次，支持第一版的批次和收益核算。

```text
lot_id
cycle_id
symbol
opened_at
quantity
remaining_quantity
entry_price
entry_fee
source_execution_id
```

### 3.5 Execution

用户实际确认发生的成交或账户事件。

```text
execution_id
portfolio_id
cycle_id
symbol
event_type
trade_time
quantity
price
gross_amount
fee
tax
net_amount
reason
advice_id
strategy_decision_id
simulation_run_id
external_ref
idempotency_key
created_at
```

第一版 `event_type`：

```text
BUY
SELL
CASH_DIVIDEND
FEE
CASH_ADJUSTMENT
CORRECTION
BONUS_SHARE
STOCK_SPLIT
RIGHTS_ISSUE
COST_ADJUSTMENT
```

公司行为不能只作为普通现金流水。`BONUS_SHARE`、`STOCK_SPLIT` 和 `RIGHTS_ISSUE` 必须通过 Lot 数量/价格调整事件处理。

建议增加：

```text
PositionLotAdjustment
adjustment_id
lot_id
event_type
quantity_delta
price_factor
event_time
reason
```

### 3.6 CashLedgerEntry

现金流水是现金余额的事实来源。

```text
entry_id
portfolio_id
entry_time
entry_type
amount
balance_after
execution_id
reason
created_at
```

`entry_type`：

```text
INITIAL
BUY
SELL
DIVIDEND
FEE
ADJUSTMENT
CORRECTION
```

### 3.7 PositionSnapshot

根据成交和行情计算的只读结果：

```text
snapshot_id
cycle_id
as_of
quantity
average_cost
cost_basis
market_price
market_value
unrealized_pnl
realized_pnl
return_rate
phase
data_context
```

## 4. 事实和计算规则

### 4.1 交易事实

用户确认的实际成交才写入 `Execution`。打开建仓页面、产生策略建议和创建模拟计划都不产生成交。

### 4.2 现金

现金余额必须可以由：

```text
初始现金 + CashLedgerEntry.amount
```

重新计算得到。

人工调整现金也必须生成 `ADJUSTMENT` 流水，不能直接覆盖余额。

### 4.3 持仓

持仓是 Execution 和 PositionLot 的聚合结果，不是用户直接编辑的事实。

### 4.4 第一版成本口径

第一版统一采用批次核算，卖出优先匹配最早未结批次（FIFO）。

要求：

1. 买入生成 PositionLot。
2. 卖出消耗 PositionLot。
3. 已实现收益按被消耗批次计算。
4. 未实现收益按剩余批次计算。
5. 手续费和税费进入收益核算。
6. 分红进入现金收益，不改变股票成本，除非显式录入成本调整。

一次卖出跨多个 Lot 时，手续费和税费按各 Lot 卖出毛金额比例分摊，并在每个 Lot 结果中保存分摊金额和 realized_pnl。

## 5. 状态流转

### 5.1 PositionCycle

```text
planned → open → closed
planned → cancelled
```

### 5.2 持仓阶段

```text
accumulating → holding
holding → left_take_profit
holding → right_trailing
holding → stopped
left_take_profit → right_trailing
left_take_profit → stopped
right_trailing → stopped
accumulating / holding / left_take_profit / right_trailing → closed
```

状态变化必须记录：

```text
old_phase
new_phase
event_type
event_time
reason
advice_id
strategy_decision_id
```

主动清仓、止损清仓、止盈清仓、纠错清仓必须使用不同事件原因。

### 5.3 幂等

重复提交相同 `idempotency_key` 必须返回原 Execution，不得重复扣现金或增加持仓。

## 6. 建仓流程

```text
Observation.ready_for_entry
→ 生成 EntryPlan
→ 用户填写实际成交信息
→ 校验策略/观察/数据上下文
→ 创建 PositionCycle
→ 创建 BUY Execution
→ 创建 PositionLot
→ 创建 CashLedgerEntry
→ 更新持仓阶段
→ 写入 PositionEvent
```

上述事实写入必须在同一数据库事务中完成。

如果用户只是打开页面、保存草稿或取消：

```text
不创建 Execution
不扣减现金
不创建 PositionCycle.open
```

## 7. 交易流程

### 买入

```text
校验现金
→ 写 BUY Execution
→ 写 PositionLot
→ 写 CashLedgerEntry(BUY)
→ 重算 PositionSnapshot
→ 根据批次推进 phase
```

### 卖出

```text
校验剩余数量
→ FIFO 消耗 PositionLot
→ 写 SELL Execution
→ 写 CashLedgerEntry(SELL)
→ 计算 realized_pnl
→ 重算 PositionSnapshot
→ 数量为 0 时关闭 Cycle
```

### 纠错

纠错不删除原始 Execution。必须新增 `CORRECTION` 事件并记录：

```text
corrected_execution_id
before_value
after_value
reason
operator
```

## 8. 收益和资产总览

持仓估值只能由 `PositionValuationService` 计算。持仓页、工作台、晨报、收益、复盘、导出和通知不得各自计算收益；缓存只是优化，过期时必须可重算。

组合总资产：

```text
cash_balance + Σ current_market_value
```

组合收益至少区分：

```text
realized_pnl
unrealized_pnl
dividend_income
fees_and_tax
net_pnl
return_rate
```

行情价格必须带：

```text
market_price_as_of
price_source
```

数据滞后时不得显示为实时资产。

## 9. API 目标

```text
GET  /api/accounts
GET  /api/portfolios/{portfolio_id}/summary
GET  /api/portfolios/{portfolio_id}/positions
GET  /api/portfolios/{portfolio_id}/cycles
GET  /api/position-cycles/{cycle_id}
POST /api/position-cycles/plan
POST /api/position-cycles/{cycle_id}/executions
POST /api/portfolios/{portfolio_id}/cash-adjustments
GET  /api/executions/{execution_id}
GET  /api/position-cycles/{cycle_id}/events
```

交易接口必须返回：

```text
execution_id
cycle_id
cash_balance
position_snapshot
realized_pnl
idempotent_replay
```

## 10. 当前能力映射

| 当前能力 | 目标归属 | 迁移方式 |
|---|---|---|
| 旧 `Position` | PositionCycle + PositionSnapshot | 一次性迁移后旧模型下线 |
| `Transaction` | Execution | 增加统一事件类型和关联字段 |
| 新 `CashLedgerEntry` | CashLedger 聚合结果 | 余额只由流水计算 |
| `ActionAdvice` | Advice reference | 交易录入时可关联 advice |
| `portfolio.manager` | Portfolio application service | 移除直接改状态的散落逻辑 |
| `position_state.py` | Phase state machine | 所有状态变化统一调用 |

## 11. 实施步骤

1. 新建目标表，不在旧表上继续堆叠字段。
2. 运行一次性迁移，将可确定的历史交易转换为新 Execution 和 CashLedgerEntry。
3. 增加 PositionCycle、PositionLot 和 PositionEvent。
4. 将建仓、买入、卖出、分红、现金调整统一经服务层处理。
5. 实现 FIFO 核算服务，并让持仓、复盘和收益共用。
6. 页面直接切换到新 API，不保留旧业务字段。
7. 对账通过后下线旧表和旧 API，禁止页面直接更新持仓状态和现金余额。

## 12. 测试与验收

1. 买入、持仓、现金在一个事务中完成。
2. 现金不足时全部回滚。
3. 重复 `idempotency_key` 不重复记账。
4. FIFO 卖出收益正确。
5. 分红和费用正确进入现金及收益。
6. 纠错保留原始事实并产生修正事件。
7. 清仓后 PositionCycle 进入 `closed`。
8. Observation 只在真实成交后变为 `promoted`。
9. 删除当前页面记录不删除历史交易事实。
10. 持仓总览和复盘使用同一收益核算结果。

---

## 实现状态与记录

### 实现状态：P1-2/P1-3 基础能力完成，交易事务已补齐

### 已完成交付物

| 文件 | 能力 | 测试 |
|---|---|---|
| `biz/portfolio.py` | Account/Portfolio/PositionCycle/PositionLot/Execution/CashLedger、FIFO、幂等、现金不足保护、公司行为基础调整 | `tests/test_biz_portfolio.py`（11） |
| `biz/valuation.py` | PositionValuationService 唯一估值入口，价格日期/来源/滞后上下文 | `tests/test_biz_valuation.py`（4） |

### 开发中遇到的问题与决策

1. 现金余额不能按业务交易时间或插入顺序取最后一条缓存；当前余额按全部 CashLedgerEntry.amount 聚合，历史补录不会覆盖当前余额。
2. `record_execution` 已收敛为单连接 `BEGIN IMMEDIATE` 事务，Execution、Lot、CashLedger 和持仓阶段更新整体提交或回滚。

### 跨模块验证

- `tests/test_biz_end_to_end.py` 已验证 Observation.ready_for_entry → PositionCycle → BUY Execution → PositionValuation 的基础链路。
- `tests/test_biz_portfolio.py` 已通过故障注入验证 Execution 插入后继续处理失败时，Execution/Lot/CashLedger 均不残留。
- `biz/workflow.py` 与 `tests/test_biz_workflow.py` 已将 EntryContext → PositionCycle → Execution → CashLedger → Observation promoted 收口为应用服务事务。
- 正式 Web 建仓入口、公司行为完整规则和 PositionSnapshot 读模型仍待完成。
- `PositionValuationService` 已持久化 `PositionSnapshot`，并由 `/api/biz/position-cycles/<cycle_id>/snapshots/<as_of>` 提供查询。
- `biz/migration.py` 已提供旧 `portfolio.db` 的只读评估、canonical code 转换、基础现金/Lot/Execution 重建、幂等导入和 `legacy_entity_map` 记录；当前仅在临时库验证，未执行生产迁移。
- `biz/workflow.py` 已提供 ready_for_entry → PositionCycle → BUY Execution → CashLedger → promoted 的单事务建仓流程。
- `web/biz_api.py` 已提供 `/api/portfolios/{portfolio_id}/positions` 和 `/api/position-cycles/{cycle_id}` 正式查询入口。
- 真实卖出已记录 `execution_lot_allocations`，按 FIFO 保存 Lot 消耗数量、成本、买入费用、卖出费用、税费和 realized_pnl。
- 观察晋级已要求关联 PositionCycle 和真实 BUY Execution；卖出原因可驱动止盈、移动止盈和止损 phase。
- 持仓汇总 API 已通过 PositionValuationService 读取当前持仓标的的 Published 行情，返回估值状态；无持仓或数据不可用时不伪造资产值。

### 后续待开发

- 建仓、买入、卖出和现金调整的核心写入已使用同一 SQLite 事务；仍需补齐跨 Observation 的建仓事务。
- PositionEvent/PositionSnapshot 完整落库。
- BONUS_SHARE/STOCK_SPLIT/RIGHTS_ISSUE 的正式事件模型和成本调整规则。
- Observation.ready_for_entry → EntryPlan → Execution 的跨模块事务。

### 跨模块验证

- `tests/test_biz_end_to_end.py` 已验证 Observation.ready_for_entry → PositionCycle → BUY Execution → PositionValuation 的基础链路。
- `tests/test_biz_portfolio.py` 已通过故障注入验证 Execution 插入后继续处理失败时，Execution/Lot/CashLedger 均不残留。
- 正式建仓 Application Service、PositionEvent/PositionSnapshot 持久化和 Observation promote 的同事务收口仍待完成。
