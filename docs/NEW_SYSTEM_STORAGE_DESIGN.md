# 新系统持久层设计 V1

## 1. 定位

本文定义新业务系统的持久化承重层：使用哪个库、有哪些表、公共实体如何落表、主键和索引如何设计、哪些字段用于版本和审计。

数据文件仍由已完成的数据模块负责。本文不重新定义 Raw、Candidate、Quality 和 Published 数据集，只定义业务系统如何引用它们。

## 2. 数据平面与业务平面

系统明确分为两个平面：

```text
数据平面
  → 数据采集、构建、质量、发布、DatasetAccess
  → 继续使用现有数据任务载体和管理元数据能力

业务平面
  → 选股、研究、策略、回测、观察、账户、持仓、收益、通知
  → 使用本文定义的新业务库和新任务入口
```

数据平面不是本次业务重写的对象。现有数据任务框架只在数据平面继续运行；业务平面不得复用其中的旧业务对象、旧运行入口或旧状态。

最终目标不是删除数据任务框架，而是：

```text
数据任务框架 = 数据平面运行基础
业务任务框架 = 新业务平面运行基础
```

两者只共享抽取到 `runtime/` 的无业务公共机制，例如任务生命周期工具、租约锁、心跳、取消令牌和错误模型；数据平面与业务平面分别持有自己的任务定义、运行实例、数据库表和 Scheduler 实例。不得直接复用旧 `ops/` 的具体任务类或旧业务表。

## 3. 新业务库

新业务系统使用独立 SQLite 数据库：

```text
output/data/business.db
```

新库只保存业务事实、业务运行记录、推导结果索引和审计数据。不得把新业务表继续写入旧 `portfolio.db`、旧 `job_runs.db`、旧 `warehouse/meta.db`。

数据模块的 `management.db` 属于数据平面，不是旧业务库；它继续承载 Dataset Registry、Published Version、Quality、Current、Source Batch 等数据事实。业务库不复制这些表，只保存不可变的 dataset reference。

数据平面的 Published Dataset 元数据由数据模块维护；业务库通过不可变引用保存。业务模块实际读取必须遵循 [数据平台与可信数据链路设计](DATA_PIPELINE_V1_DESIGN.md)，不能假设所有配置数据集已经具备统一读取实现：

```text
dataset_name
dataset_version
data_as_of
quality_status
```

## 4. 表与实体映射

### 4.1 配置与策略

| 实体 | 表 |
|---|---|
| Strategy | `strategies` |
| StrategyVersion | `strategy_versions` |
| StrategyValidation | `strategy_validations` |
| Indicator/Rule 使用引用 | `strategy_dependencies` |
| MarketRegime | `market_regimes` |

### 4.2 选股与研究

| 实体 | 表 |
|---|---|
| Screen | `screens` |
| ScreenVersion | `screen_versions` |
| UniverseSnapshot | `universe_snapshots` |
| ScreenRun | `screen_runs` |
| ScreenCandidate | `screen_candidates` |
| ResearchRun | `research_runs` |
| ResearchEvidence | `research_evidence` |
| ResearchReport | `research_reports` |

### 4.3 模拟

| 实体 | 表 |
|---|---|
| SimulationPlan | `simulation_plans` |
| SimulationRun | `simulation_runs` |
| SimulationFill | `simulation_fills` |
| SimulationEvent | `simulation_events` |
| SimulationPosition/Lot | `simulation_positions` / `simulation_lots` |
| SimulationResult | `simulation_results` |
| ParameterSearchRun | `parameter_search_runs` |
| ParameterSearchResult | `parameter_search_results` |

### 4.4 观察与账户

| 实体 | 表 |
|---|---|
| WatchSubscription | `watch_subscriptions` |
| Observation | `observations` |
| ObservationSource | `observation_sources` |
| ObservationSnapshot | `observation_snapshots` |
| ObservationEvent | `observation_events` |
| Account | `accounts` |
| Portfolio | `portfolios` |
| PositionCycle | `position_cycles` |
| PositionLot | `position_lots` |
| Execution | `executions` |
| CashLedgerEntry | `cash_ledger_entries` |
| PositionLotAdjustment | `position_lot_adjustments` |
| PositionEvent | `position_events` |
| PositionSnapshot | `position_snapshots` |

### 4.5 建议、通知与复盘

| 实体 | 表 |
|---|---|
| Advice | `advices` |
| NotificationEvent | `notification_events` |
| NotificationDelivery | `notification_deliveries` |
| DailyReport | `daily_reports` |
| PerformanceSnapshot | `performance_snapshots` |
| PerformanceComparison | `performance_comparisons` |
| PositionCycleReview | `position_cycle_reviews` |
| ReviewEvidence | `review_evidence` |

### 4.6 业务任务与审计

| 实体 | 表 |
|---|---|
| BusinessTaskDefinition | `business_task_definitions` |
| BusinessTaskConfigVersion | `business_task_config_versions` |
| BusinessExecutionRequest | `business_execution_requests` |
| BusinessJobRun | `business_job_runs` |
| BusinessJobEvent | `business_job_events` |
| BusinessTaskLock | `business_task_locks` |
| Artifact | `artifacts` |
| Lineage | `artifact_lineage` |
| AuditEvent | `audit_events` |
| SchemaMigration | `schema_migrations` |

## 5. 公共字段规则

所有业务表按需包含：

```text
id
created_at
updated_at
```

所有推导结果必须包含：

```text
data_as_of
dataset_refs_json
strategy_version_id
input_hash
```

所有状态表必须包含：

```text
status
status_changed_at
last_error_code
last_error_message
```

时间统一使用带时区 UTC 时间戳；业务日期单独保存为 `YYYY-MM-DD`，不得用本地时间戳替代交易日期。

## 6. 关键表结构与约束

### 6.1 strategy_versions

```text
strategy_version_id PRIMARY KEY
strategy_id NOT NULL
version_no NOT NULL
config_json NOT NULL
config_hash NOT NULL
status NOT NULL
published_at
enabled_at
created_at NOT NULL
UNIQUE(strategy_id, version_no)
UNIQUE(strategy_id, config_hash)
```

### 6.2 screen_runs

```text
screen_run_id PRIMARY KEY
screen_version_id NOT NULL
universe_snapshot_id NOT NULL
data_context_json NOT NULL
requested_as_of NOT NULL
actual_data_as_of
status NOT NULL
matched_count NOT NULL
started_at
finished_at
```

### 6.3 screen_candidates

```text
candidate_id PRIMARY KEY
screen_run_id NOT NULL
symbol NOT NULL
rank_no
score
condition_results_json NOT NULL
display_values_json NOT NULL
data_as_of NOT NULL
UNIQUE(screen_run_id, symbol)
INDEX(screen_run_id, rank_no)
```

### 6.4 research_runs

```text
research_run_id PRIMARY KEY
subject_type NOT NULL
symbol
observation_id
source_screen_run_id
source_candidate_id
strategy_version_id
data_context_json NOT NULL
status NOT NULL
result_json
started_at
finished_at
```

### 6.5 strategy_decisions

StrategyDecision 是跨模块公共实体，单独落表：

```text
decision_id PRIMARY KEY
strategy_version_id NOT NULL
research_run_id
simulation_run_id
observation_id
position_cycle_id
symbol NOT NULL
decision_time NOT NULL
data_as_of NOT NULL
action NOT NULL
quantity
quantity_ratio
price
stop_price
target_price
input_snapshot_json NOT NULL
decision_trace_json NOT NULL
reason
valid_until
created_at NOT NULL
INDEX(symbol, decision_time)
INDEX(strategy_version_id, data_as_of)
```

`decision_trace_json` 必须包含：

```text
evaluated_rules
triggered_rules
suppressed_rules
final_action
```

### 6.6 simulation_fills

统一使用单边模拟成交：

```text
simulation_fill_id PRIMARY KEY
simulation_run_id NOT NULL
symbol NOT NULL
side NOT NULL                 # BUY / SELL
signal_time NOT NULL
execution_time
signal_price
execution_price
quantity NOT NULL
gross_amount NOT NULL
fee NOT NULL
tax NOT NULL
slippage NOT NULL
decision_id
reason
INDEX(simulation_run_id, execution_time)
INDEX(simulation_run_id, symbol, execution_time)
```

不保存成对 `buy_price/sell_price/pnl/pnl_pct` 作为成交事实。

### 6.7 observations

```text
observation_id PRIMARY KEY
symbol NOT NULL
status NOT NULL
current_strategy_version_id
latest_simulation_run_id
latest_research_run_id
promoted_position_cycle_id
started_at NOT NULL
expires_at
updated_at NOT NULL
```

### 6.8 position_cycles

```text
position_cycle_id PRIMARY KEY
portfolio_id NOT NULL
symbol NOT NULL
status NOT NULL                 # planned/open/closed/cancelled
phase NOT NULL                  # accumulating/holding/...
strategy_version_id
observation_id
simulation_run_id
entry_plan_snapshot_json
opened_at
closed_at
```

### 6.9 executions

真实成交同样采用单边事件：

```text
execution_id PRIMARY KEY
portfolio_id NOT NULL
position_cycle_id
symbol NOT NULL
event_type NOT NULL
trade_time NOT NULL
quantity
price
gross_amount
fee NOT NULL
tax NOT NULL
net_amount NOT NULL
advice_id
decision_id
idempotency_key NOT NULL
UNIQUE(portfolio_id, idempotency_key)
```

### 6.10 cash_ledger_entries

```text
cash_entry_id PRIMARY KEY
portfolio_id NOT NULL
entry_type NOT NULL
amount NOT NULL
execution_id
balance_after NOT NULL
entry_time NOT NULL
UNIQUE(portfolio_id, execution_id, entry_type)
```

### 6.11 task_locks

```text
lock_key PRIMARY KEY
owner_run_id NOT NULL
acquired_at NOT NULL
heartbeat_at NOT NULL
expires_at NOT NULL
```

## 7. 账户初始化和存量处理

新系统启动时创建默认账户：

```text
Account(real)
└── Portfolio(default)
```

历史 `portfolio.db` 不直接作为新库运行数据。迁移前先生成报告：

```text
可确定迁移
可重建迁移
无法确定
```

无法确定的历史成本、Lot、策略版本和建议关系必须标记 `unknown/reconstructed`，不能伪造精确事实。

## 8. 迁移和删除顺序

```text
旧库只读备份
→ 新库建表
→ 一次性导入可确定事实
→ 生成重建结果
→ 对账
→ 切换新服务
→ 旧库归档只读
→ 验证通过后删除旧入口和旧表
```

删除旧数据属于破坏性操作，必须在执行前获得明确确认并说明不可恢复范围。

## 9. 验收

1. 新业务事实和业务运行记录只连接 `business.db`。
2. 数据模块继续维护 `management.db`，业务侧只通过 `DatasetAccess` 读取数据。
2. 所有公共实体都有明确落表。
3. 所有外键和唯一约束经过测试。
4. StrategyDecision、ResearchRun、SimulationRun、Observation、PositionCycle 可以完整关联。
5. 单边模拟成交可以重算持仓和收益。
6. 真实 Execution 可以重算现金和持仓。
7. 重复请求不会产生重复成交或重复扣款。
8. 新系统删除旧库连接后仍可启动和完成核心冒烟流程。
