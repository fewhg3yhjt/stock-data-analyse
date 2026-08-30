# 投资产品领域模型与统一契约 V1

## 1. 目的与真源

本文是所有业务子模块的公共契约，优先级高于历史设计中的同名概念。

```text
新体系 = 唯一产品领域、运行时模型、公共术语、状态和 API 真源
旧体系 = 一次性历史数据迁移输入，迁移完成后下线
```

旧体系不得进入新运行时，不得作为 fallback、双写目标、业务查询来源或第二套协议。历史数据如需保留，只能通过一次性迁移或只读归档处理。

数据模块的 Raw、Candidate、Quality、Published、Current 视为既有数据平面能力；业务模块必须按 [数据平台与可信数据链路设计](DATA_PIPELINE_V1_DESIGN.md) 消费其真实输出，不得假设尚未实现的统一数据集接口。

## 2. 旧体系下线规则

禁止：

```text
新旧模型双写
新旧模型双读
新模型失败回退旧模型
新旧状态互相同步
新旧引擎按场景分流
旧对象继续作为业务查询来源
```

唯一允许的历史处理过程：

```text
冻结旧体系
→ 备份和评估历史数据
→ 一次性迁移可确定数据
→ 不确定数据标记 unknown/reconstructed
→ 完成对账
→ 切换新模型
→ 旧表、旧 API、旧运行入口下线
```

## 3. 新体系对象
新系统只允许以下对象进入运行时：

```text
IndicatorDefinition / IndicatorVersion / IndicatorValue
Condition / Rule / Strategy / StrategyVersion
StrategyContext / StrategyDecision
Screen / ScreenRun / ScreenCandidate
ResearchRun / ResearchEvidence / ResearchReport
SimulationPlan / SimulationRun / SimulationFill / SimulationEvent
Observation / WatchSubscription / ObservationSnapshot
Account / Portfolio / PositionCycle / PositionLot
Execution / CashLedgerEntry / PositionSnapshot
Advice / NotificationEvent / NotificationDelivery
PerformanceSnapshot / ReviewEvidence / PositionCycleReview
```

旧对象只允许由一次性迁移程序读取，不进入新服务接口。迁移完成后必须删除旧模型、旧表、旧运行入口和旧 API，不保留长期兼容层。

## 4. 指标、规则与策略的唯一关系

```text
DatasetResult.data/context
→ IndicatorContext
→ RuleRegistry
→ SchemeConfig
→ CompiledStrategy
→ StrategyDecision
```

### 4.1 IndicatorContext

`IndicatorContext` 是新系统唯一指标取值上下文，负责在已读取的行情 DataFrame 上计算指标值和序列；数据版本、质量和来源来自 `DatasetResult.context`，不在 IndicatorContext 内重复定义。

### 4.2 RuleRegistry

`RuleRegistry` 是新系统唯一规则和条件执行入口。`ConditionSpec` 只是传给 Registry 的结构化配置，不是独立执行引擎。

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

### 4.3 SchemeConfig

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

### 4.4 StrategyContext

策略、选股、研究和模拟统一使用 `StrategyContext` 作为运行时输入。不得再创建 `RuleContext` 或 `EvaluationContext` 作为平行公共类型。

```text
symbol
evaluation_time
data_as_of
market_data
indicator_context
fundamental_values
market_regime
position_state
position_quantity
cash_available
previous_decisions
```

### 4.5 MarketRegime

市场状态是研究和策略的共同输入事实，不是策略协议：

```text
regime_id
regime
as_of
confidence
algorithm_version
input_snapshot
explanation
data_context
```

第一版状态集合：

```text
strong_bull
overbought_bull
weak_bull
range
weak_bear
strong_bear
```

策略通过 `StrategyRegimePolicy` 决定如何使用该事实。

MarketRegime 由业务平面的 `MarketRegimeService` 生产，不由数据采集任务生产，也不由每个研究请求各自私算一套。其输入是数据模块提供的 `DatasetResult.data/context`，默认基于已发布 `stock_daily`/`indicators` 数据计算，结果写入业务库 `market_regimes`；实际数据读取入口和字段边界以 `DATA_PIPELINE_V1_DESIGN.md` 为准。

```text
DatasetResult(stock_daily/indicators)
→ MarketRegimeService
→ business.db.market_regimes
→ StrategyContext.market_regime
```

研究、选股、Simulation 和 LiveAdvice 使用同一 `MarketRegime` 记录；如果指定历史 `as_of` 没有对应记录，先按同一算法版本计算并持久化，不能由调用方临时采用另一套状态口径。

## 6. 公共实体

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

## 7. 决策解释轨迹

每次策略评估必须区分：

```text
evaluated_rules
triggered_rules
suppressed_rules
```

`suppressed_rules` 保存满足但被更高优先级规则、风险约束或持仓状态抑制的规则及原因，用于解释“为什么没有买入/加仓”。

## 8. 状态真源

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

## 9. 任务框架接入边界

以下业务长任务必须走新平台的 `BusinessTaskDefinition → BusinessRequest → BusinessJobRun`：

```text
screen.run
research.run
simulation.run
parameter_search.run
report.daily_generate
```

以下业务轻量维护任务也必须写业务运行记录：

```text
observation.expiry_reconcile
advice.refresh
notification.outbox_delivery
health.reconcile
```

真实交易不是后台任务，走同步数据库事务：

```text
portfolio.execution.create
portfolio.cash.adjust
observation.state_transition
```

任务清单以本文为唯一准则，平台文档只能引用，不得重新定义另一份清单。

## 10. 迁移规则

1. 迁移脚本只读旧对象，目标只写新对象。
2. 迁移必须幂等并输出差异报告。
3. 无法推断的历史字段标记为 `reconstructed` 或 `unknown`。
4. 不得用当前状态伪造历史事件。
5. 迁移完成后旧表、旧 API 和旧运行入口下线。
6. 新业务不得依赖旧对象存在。

## 11. 全链路验收

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

每个推导结果都能追溯到策略版本、数据上下文、输入快照和规则评估轨迹。真实输入契约以 `DATA_PIPELINE_V1_DESIGN.md` 为准。
