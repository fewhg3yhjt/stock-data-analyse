# 投资产品领域模型与统一契约 V1

## 1. 目的与真源

本文是所有业务子模块的公共契约，优先级高于历史设计中的同名概念。

```text
新体系 = 产品领域、公共术语、状态和 API 的唯一真源
现有旧体系 = 可复用代码骨架和迁移来源
```

旧体系不整体删除，但不再作为第二套产品协议长期并行。后续实现必须把旧对象映射到本文定义的对象，并逐步停止旧入口。

数据模块的 Raw、Candidate、Quality、Published、Current、DataContext 实现和口径视为已完成。业务模块只消费其已发布结果，不再引入新的数据生产设计。

## 2. 新旧对象映射

| 旧体系 | 新体系 | 处理结论 |
|---|---|---|
| `Feature` / `metric_key` | `IndicatorDefinition` / `IndicatorRef` | 保留实现，统一名称和版本 |
| `IndicatorContext` | `EvaluationContext` 的指标提供者 | 保留并作为唯一指标取值入口 |
| `RuleExecutor` / `RuleRegistry` | `RuleDefinition` / 规则执行注册中心 | 保留并作为唯一条件、规则执行入口 |
| `SchemeConfig` | `StrategyVersionConfig` | 保留并作为策略版本配置载体 |
| `StockSet` | `ScreenDefinition` + `UniverseSnapshot` | 拆分配置和运行时集合 |
| `stock_set_members` | `ScreenCandidate` | 保存筛选运行中的候选明细 |
| `signals` | `StrategyDecision` | 增加输入值、版本和解释快照 |
| `SimulationPlan` | `SimulationPlan` | 保留并补齐来源和版本 |
| `SimulationRun` | `SimulationRun` | 作为唯一回测/模拟运行实体 |
| `SimulationTrade` | `SimulationTrade` | 保留并统一成交语义 |
| `SimulationEvent` | `SimulationEvent` | 保留并统一事件类型 |
| `trade_ledger` | `Execution` + `CashLedgerEntry` | 交易事实和现金事实拆分 |
| `positions` | `PositionCycle` + `PositionLot` + `PositionSnapshot` | 当前持仓改为聚合结果 |
| `domain/`、`services/`、`repositories/backend_domain.py` | 新领域服务和仓储迁移基础 | 逐项对账，不新建平行骨架 |

## 3. 指标、规则与策略的唯一关系

```text
Published Dataset
→ IndicatorContext
→ RuleRegistry
→ SchemeConfig
→ CompiledStrategy
→ StrategyDecision
```

### 3.1 IndicatorContext

`IndicatorContext` 是唯一指标取值上下文，负责指标值、序列、时间边界、依赖、缺失值、版本和来源；不负责买卖动作。

### 3.2 RuleRegistry

`RuleRegistry` 是唯一规则和条件执行入口。新文档中的 `ConditionSpec` 只是传给 Registry 的结构化配置，不是新执行引擎。

第一版规则包括：

```text
comparison
cross
between
consecutive
count
and
or
not
```

每个规则执行器必须返回：

```text
passed
evaluation_status
actual_values
threshold_values
explanation
dependencies
```

### 3.3 SchemeConfig

`SchemeConfig` 是唯一策略配置载体。不得新增平行的 `StrategySpec` 存储模型。它通过不可变版本表达：

```text
strategy_id
strategy_version
entry_rules
exit_rules
risk
position_sizing
execution
benchmark
config_hash
```

`CompiledStrategy` 是经过校验和规则解析后的内存运行对象，不持久化、不读取文件、数据库或网络。

## 4. 公共实体

### DataContext

```text
dataset_versions
indicator_versions
data_as_of
requested_start
requested_end
quality_status
source
fallback_used
is_stale
```

### StrategyDecision

```text
decision_id
strategy_id
strategy_version
symbol
decision_time
data_as_of
action
quantity_ratio
price
stop_price
target_price
input_dependencies
input_snapshot
decision_trace
reason
valid_until
```

`input_snapshot` 必须保存当时实际使用的指标和上下文值，不得只保存指标名称。

### ResearchRun

一次可追溯的个股或 Observation 研究运行，具体定义见 `RESEARCH_AND_ANALYSIS_DESIGN.md`。

### SimulationRun

一次统一回测/模拟运行，锁定策略、数据、资金、执行规则和基准。

### Observation

投资观察周期，不等于筛选候选或用户关注关系。

### PositionCycle

一次真实建仓到清仓的周期。

### Execution

用户确认实际发生的交易或账户事件。

### Advice

由 `StrategyDecision` 转换而来的面向用户的可执行建议。

## 5. 决策解释轨迹

每次策略评估必须区分：

```text
evaluated_rules
triggered_rules
suppressed_rules
```

`suppressed_rules` 保存满足但被更高优先级规则、风险约束或持仓状态抑制的规则及原因，用于解释“为什么没有买入/加仓”。

## 6. 状态真源

### 策略版本

```text
draft → validated → published → enabled
                              └→ disabled → archived
```

### 长任务

```text
requested → running → success
                    ├→ partial_success
                    ├→ failed
                    └→ cancelled
```

### Observation

```text
discovered → observing → ready_for_entry → promoted
                  ├→ paused
                  ├→ expired
                  └→ abandoned → archived
```

Simulation 的执行状态属于 `SimulationRun`，不重复塞入 Observation。

### PositionCycle

```text
planned → open → closed
planned → cancelled
```

## 7. 任务框架接入边界

以下长任务必须走现有 `ops/` 的 `TaskDefinition → Request → JobRun`：

```text
screen.run
research.run
simulation.run
parameter_search.run
report.daily_generate
```

以下轻量维护任务也必须写运行记录：

```text
observation.expiry_reconcile
advice.refresh
notification.outbox_delivery
health.reconcile
```

真实交易不是后台任务，走同步数据库事务。

## 8. 迁移规则

1. 旧对象先建立映射，不直接删除。
2. 旧表和旧字段进入只读兼容阶段。
3. 新业务只写新对象或兼容适配层。
4. 迁移必须幂等并输出差异报告。
5. 无法推断的历史字段标记为 `reconstructed` 或 `unknown`。
6. 不得用当前状态伪造历史事件。
7. 不得为了迁移方便把不同语义的旧对象简单改名。

## 9. 全链路验收

必须能够追踪：

```text
ScreenCandidate
→ ResearchRun
→ StrategyDecision
→ SimulationRun
→ Observation
→ PositionCycle
→ Execution
→ Advice
→ NotificationEvent
→ Performance / Review
```

每个推导结果都能追溯到策略版本、数据上下文、输入快照和规则评估轨迹。
