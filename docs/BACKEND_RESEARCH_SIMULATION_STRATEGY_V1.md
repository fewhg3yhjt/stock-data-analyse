# 股票研究、模拟、策略、持仓与通知后台设计及实现方案 V1.0

> 废弃文档。禁止用于新功能、接口和运行时实现。仅允许用于历史数据评估和一次性迁移；当前领域模型和跨模块契约以 `PRODUCT_BLUEPRINT.md` 与 `DOMAIN_MODEL_AND_CONTRACTS.md` 为准。

> 文档性质：项目内部后台领域设计方案。
>
> 适用环境：现有 Python + Parquet + SQLite 架构，服务器约 2C2G。
>
> 边界：只设计后台对象、状态机、服务边界和运行流程，不讨论页面、导航或交互。

## 1. 文档目的与边界

本方案目标是在现有数据链路基础上，统一“指标/特征、条件、规则、股票关系、模拟、策略运行、真实交易账本、持仓状态、收益复盘、消息通知”的后台模型与运行流程，并保证在 2C2G 机器上可稳定实现。

- 保留完整领域关系和必要状态机，但不引入重型基础设施。
- 研究阶段不要求先创建策略；可以先选择股票、创建模拟方案、验证收益，再决定是否沉淀为策略。
- 真实交易当前仅支持人工录入/维护，不支持自动下单；模拟和策略运行必须与真实持仓严格隔离。
- 股票本身不设置“观察/自选/持仓/候选”的万能状态；这些是关系或独立业务对象。
- 所有指标/条件由统一引擎解释，避免模拟、策略、K线、通知各自重复计算。

## 2. 与现有数据链路的衔接

业务层只能消费已发布的数据版本，不直接调用外部行情接口作为正式事实来源。

```text
外部数据源
  -> Raw Batch
  -> 标准数据集（daily / industry / fundamentals / ...）
  -> 质量检查 + Publish
  -> 统一数据访问服务
  -> 特征计算
  -> 条件/规则
  -> 研究 / 模拟 / 策略 / 持仓 / 通知
```

建议存储：

| 数据 | 建议存储 | 用途 |
|---|---|---|
| 全市场日线、行业日线、特征快照 | Parquet（月分区） | 批量计算、条件筛选、回测 |
| Raw 日线/分钟批次 | Parquet（按日+批次） | 追溯、回补 |
| 分钟/在线行情 | Parquet/短期缓存（按日） | 观察、持仓运行状态、通知 |
| 规则、策略、关系、状态、任务 | SQLite | 业务定义与运行控制 |
| 交易流水、模拟结果、通知事件 | SQLite（必要明细可分表） | 审计、复盘、状态流转 |

## 3. 后台领域模型总览

```text
Dataset / Published Version
  -> 特征定义
  -> 特征快照 / 持仓上下文特征
  -> 条件
  -> 规则表达式
  -> 研究筛选 / 模拟 / 通知

研究筛选 -> 股票集合

模拟方案 -> 模拟运行 -> 模拟交易/事件
  -> 验证有效
  -> 策略定义 -> 策略版本 -> 运行配置 -> 策略运行
  -> 候选 / 买卖信号
  -> 模拟持仓 / 人工决策

实际交易流水 -> 当前持仓 -> 持仓运行状态 -> 卖出提醒/通知

复盘：个人收益 vs 策略收益 vs 大盘收益
```

核心关系原则：股票是主数据对象；“自选、观察来源、策略命中、持仓、模拟交易”全部通过关系或独立记录关联到股票。同一股票可同时被多个股票集合、多个策略、多个模拟任务使用。

## 4. 特征/指标体系

用户层仍可称“指标”，业务后台统一按 Feature（特征）引用，但不再建立第二套指标字段标准。指标字段的唯一标准来源是项目现有的 `config/metrics/catalog.yaml`；后台 Feature 只引用其中的 `key`，并补充业务上下文和用途约束。

### 4.1 指标标准边界

`config/metrics/catalog.yaml` 是指标字段目录和生产元数据的权威来源，至少包含：

- `key`：稳定的机器字段名，规则和业务对象只引用此字段。
- `name`：展示名称。
- `category`：基础行情、估值、基本面、技术指标、研究因子或业务指标。
- `definition`：统一口径说明。
- `unit`：数值单位。
- `dataset` / `output_column`：派生指标的来源数据集和产出列。
- `min_history`：计算所需最小历史窗口。
- `applies_to`：适用证券类型。
- `producer_task`：生产该指标的任务。
- `builtin` / `editable`：是否内置及是否允许编辑。

本后台不修改 `indicators/`、`warehouse/indicators_build.py`、指标配置或指标构建任务，也不复制这些字段定义。新增 Feature 时必须先检查 catalog 中是否已有对应 `key`：

1. 已有标准字段：直接引用 `key`，不得另起同义字段。
2. 尚无标准字段：先由指标统一收口项目增加 catalog 定义和生产实现；后台只在标准字段可消费后接入。
3. 持仓上下文特征：只有在 catalog 已登记或由本后台明确登记为业务指标后，才能进入规则引用。

因此，后台 Feature Definition 是“业务可用性投影”，不是指标生产定义。它负责声明 `required_context`、`supported_usages` 和版本快照，不负责重新定义指标公式。

| 类型 | 例子 | 生成范围 |
|---|---|---|
| 证券特征 | MA5、MA10、ATR、RS、距离压力区、成交量比 | 全市场/指定股票批量计算 |
| 板块/市场特征 | 板块排名、回流、驻留度、市场风格 | 行业/市场 |
| 持仓上下文特征 | 持仓收益、买入后最高价、高点回撤、持仓天数 | 仅对持有中的持仓计算 |

### 4.2 feature_definitions

- `feature_id`
- `metric_key`：对应 `config/metrics/catalog.yaml` 的稳定 `key`；持仓上下文特征也必须有明确稳定键
- `name`
- `category`: `TECHNICAL` / `MARKET` / `INDUSTRY` / `POSITION` / `FUNDAMENTAL`
- `value_type`: `NUMBER` / `BOOLEAN` / `STRING`
- `required_context`: `SECURITY` / `INDUSTRY` / `MARKET` / `POSITION`
- `supported_usages`: `SCREEN` / `BUY` / `SELL` / `NOTIFY`（JSON 数组）
- `source_datasets`
- `calculation_key`
- `lookback_days`
- `frequency`: `DAILY` / `REALTIME`
- `version`
- `enabled`
- `created_at` / `updated_at`

`feature_id` 是后台业务对象标识，`metric_key` 是指标标准字段标识，两者不混用。适用范围必须由后台元数据控制。例如“持仓后高”引用 catalog 的 `position_peak_price`，要求 `POSITION` 上下文，且只支持 `SELL` / `NOTIFY`，因此买入规则即使绕过前端直接调用接口，也必须被后端校验拒绝。

### 4.3 统一计算范式

1. 每个新增业务特征先关联标准 `metric_key`，再登记后台可用性，不允许页面或策略自己临时计算同名指标。
2. 日频证券特征统一生成 Feature View，所有条件选股和启用策略共享。
3. 持仓上下文特征按持有中的 Position 增量维护，不为全市场生成。
4. 指标 catalog 负责生产口径版本；后台 Feature Definition 负责业务用途版本。回测/策略运行同时保存两者的版本快照。

## 5. 条件与规则引擎

条件是原子判断，规则是条件树。选股、买入、卖出、消息通知必须复用同一规则引擎。

示例：前高下方蓄势。

```text
板块回流 = true
AND RS改善 = true
AND 距压力区 BETWEEN 0.01 AND 0.04
AND 波动压缩 = true
AND 热度比例 < 0.70
```

规则表达式建议 JSON：

```json
{
  "operator": "AND",
  "children": [
    {"feature": "industry_reflow", "operator": "IS_TRUE"},
    {"feature": "rs_improving", "operator": "IS_TRUE"},
    {"feature": "distance_to_pressure", "operator": "BETWEEN", "value": [0.01, 0.04]}
  ]
}
```

生命周期原则：

| 对象 | 状态 | 是否需要状态机 |
|---|---|---|
| 条件 | 无独立生命周期 | 否 |
| 规则 | 草稿 / 可用 / 已归档 | 轻量状态即可 |
| 特征 | 启用 / 停用 + 版本 | 否，不做业务状态机 |

## 6. 股票关系与研究对象

### 6.1 股票集合

股票集合是“本次研究哪些股票”的稳定载体，不等于策略。来源可以是手工勾选、条件筛选、自选、策略命中结果或其他扫描。一个股票可属于任意多个集合。

`stock_sets`：

- `stock_set_id`
- `name`
- `description`
- `source_type`: `MANUAL` / `SCREEN` / `WATCHLIST` / `STRATEGY_MATCH` / `OTHER`
- `source_ref_id`
- `created_at` / `updated_at`

`stock_set_members`：

- `stock_set_id`
- `instrument`
- `added_at`
- `PRIMARY KEY(stock_set_id, instrument)`

### 6.2 自选关系

自选是长期人工关注关系。后台建议单独保存 `watchlist` / `watchlist_members`，不与策略候选状态混用。用户删除自选只删除关系，不影响该股票在持仓、模拟或策略命中中的记录。

### 6.3 观察视图

观察不是一份独立股票主表，而是一个聚合查询/物化视图。

```text
Observation View =
  ACTIVE Positions
  UNION Active Watchlist Members
  UNION Active Strategy Candidates
  UNION Temporary Watch Items
```

结果按 `instrument` 去重，同时保留 `source_tags`，例如 `持仓`、`自选`、`策略候选`。

## 7. 模拟/回测体系

模拟不是策略的附属品。模拟的输入是“股票集合 + 买入规则 + 卖出规则 + 执行/仓位/成本参数 + 时间范围”。规则可以是临时规则，也可以引用已保存规则。模拟结果有效后，才可选择保存为正式策略。

### 7.1 simulation_plans

- `plan_id`
- `name`
- `stock_set_id`
- `buy_rule_id` / `buy_rule_snapshot`
- `sell_rule_id` / `sell_rule_snapshot`
- `start_date` / `end_date`
- `initial_capital`
- `position_rule_json`
- `execution_rule_json`
- `risk_rule_json`
- `broker_fee_profile_id`
- `created_at` / `updated_at`

### 7.2 模拟任务状态机

```text
待运行 -> 运行中 -> 成功 / 失败 / 已取消
```

状态修改必须由 `simulation_service` 统一执行，禁止其他模块直接 `UPDATE status`。2C2G 下大型回测并发固定为 1，其余任务进入待运行队列。

### 7.3 模拟运行数据

`simulation_runs`：

- `run_id`
- `plan_id`
- `status`
- `data_versions_json`
- `feature_versions_json`
- `started_at` / `finished_at`
- `progress`
- `result_summary_json`
- `error_summary`

`simulation_trades`：

- `trade_id` / `run_id` / `instrument`
- `buy_signal_date` / `buy_date` / `buy_price`
- `sell_signal_date` / `sell_date` / `sell_price`
- `quantity`
- `fees`
- `pnl` / `pnl_pct`
- `buy_reason_json` / `sell_reason_json`

`simulation_events`：

- `event_id` / `run_id` / `event_time` / `instrument`
- `event_type`
- `payload_json`

模拟必须保存逐笔交易及事件原因，不能只保存最终收益。

### 7.4 基准与收益

- 策略净值与累计收益。
- 同期基准净值（沪深300、中证1000等由方案指定）。
- 超额收益、最大回撤、胜率、盈亏比、Profit Factor、平均持仓天数。
- 所有收益必须扣除所配置的交易成本。

## 8. 策略体系

正式策略是模拟验证后沉淀的可复用规则组合。完整策略至少包含股票范围、买入规则、卖出规则、仓位规则、风险规则和执行规则。

```text
Strategy =
  Universe Rule
  + Buy Rule
  + Sell Rule
  + Position Rule
  + Risk Rule
  + Execution Rule
```

### 8.1 策略状态机

```text
草稿 -> 已启用 -> 已停用 -> 可再次启用
草稿 / 已停用 -> 已归档
```

- 草稿：允许编辑，不参与自动运行。
- 已启用：由调度器运行固定版本。
- 已停用：保留配置和历史，不产生新的运行。
- 已归档：历史只读，不允许直接重新启用；如需恢复应复制为新策略/版本。

### 8.2 策略版本

`strategies`：

- `strategy_id`
- `name`
- `status`
- `current_version`

`strategy_versions`：

- `strategy_id`
- `version`
- `universe_rule_snapshot`
- `buy_rule_snapshot`
- `sell_rule_snapshot`
- `position_rule_json`
- `risk_rule_json`
- `execution_rule_json`
- `source_simulation_run_id`
- `created_at`
- `PRIMARY KEY(strategy_id, version)`

已启用版本禁止原地修改。修改策略时创建新版本，策略运行记录必须锁定 `strategy_id + version`。

## 9. 策略自动运行与候选状态

轻量策略运行方式：

```text
日线数据 Publish
  -> 统一 Feature Builder（全市场只算一次）
  -> Feature View（月度 Parquet）
  -> Strategy Runner 单进程读取所有已启用策略
  -> 按策略串行过滤 Feature View
  -> strategy_runs + strategy_matches
  -> 需要持续观察的结果生成 Candidate
```

禁止每个策略启动独立进程。策略本身尽量只是对 Feature View 做过滤和状态判断。

`strategy_runs`：

- `run_id`
- `strategy_id` / `strategy_version`
- `trading_date`
- `status`: `待运行` / `运行中` / `成功` / `部分成功` / `失败`
- `data_versions_json`
- `started_at` / `finished_at`
- `matched_count`
- `error_summary`

`strategy_matches`：

- `match_id` / `run_id` / `instrument`
- `match_type`
- `rule_id`
- `feature_snapshot_json`
- `created_at`

`strategy_candidates` 状态机：

```text
观察中 -> 已确认 / 已失效 / 已过期
```

`strategy_candidates`：

- `candidate_id`
- `strategy_id` / `strategy_version`
- `instrument`
- `status`
- `created_at` / `expires_at` / `updated_at`
- `source_run_id`
- `context_json`

## 10. 信号与执行边界

买入规则/卖出规则只负责产生“判断结果”，绝不直接改变真实持仓。第一版可使用轻量 `signals` 记录保存可审计的买卖判断。

`signals`：

- `signal_id`
- `signal_type`: `BUY` / `SELL`
- `source_type`: `SIMULATION` / `STRATEGY` / `POSITION_RULE`
- `source_ref_id`
- `instrument`
- `strategy_id` / `version`（可空）
- `reason_json`
- `generated_at`

边界：

| 运行场景 | 买/卖信号之后允许的动作 | 禁止动作 |
|---|---|---|
| 历史模拟 | 创建/关闭模拟持仓，计算资金曲线 | 真实下单 |
| 模拟运行 | 创建/关闭模拟持仓，可通知 | 真实下单 |
| 真实提醒 | 产生通知，等待人工操作 | 自动改变真实持仓、真实下单 |

## 11. 真实交易账本与持仓

交易流水是事实，持仓是结果。真实操作必须以不可随意覆盖的交易流水记录买入、卖出、加仓、减仓和费用；当前持仓、平均成本、已实现盈亏、未实现盈亏由流水推导或增量维护。

`trade_ledger`：

- `trade_id`
- `account_id`（第一版可固定默认账户）
- `instrument`
- `trade_time`
- `side`: `BUY` / `SELL`
- `price`
- `quantity`
- `commission`
- `stamp_tax`
- `transfer_fee`
- `other_fee`
- `total_fee`
- `source`: `MANUAL` / `IMPORT`
- `related_strategy_id`（可空）
- `related_signal_id`（可空）
- `note`
- `created_at`

`positions`：

- `position_id`
- `instrument`
- `position_type`: `MANUAL_REAL` / `PAPER` / `SIMULATION`
- `strategy_id` / `version`（可空）
- `open_time`
- `status`: `持有中` / `已清仓`
- `quantity`
- `available_quantity`
- `avg_cost`
- `realized_pnl`
- `total_fees`
- `closed_at`
- `created_at` / `updated_at`

真实 `MANUAL_REAL` Position 的变动只能由真实交易流水或明确的人工修正操作驱动；策略 SELL Signal 只能提示，不能将其直接改为已清仓。

`position_runtime_states`：

- `position_id`
- `current_price`
- `highest_since_entry`
- `lowest_since_entry`
- `unrealized_pnl`
- `unrealized_pnl_pct`
- `max_profit_pct`
- `drawdown_from_high`
- `holding_days`
- `updated_at`

只为“持有中”的持仓维护这些字段。持仓清仓后停止实时更新，但历史值和交易流水保留。

## 12. 交易成本与费用配置

`broker_fee_profiles`：

- `profile_id`
- `name`
- `commission_rate`
- `min_commission`
- `stamp_tax_rate`
- `transfer_fee_rate`
- `other_rules_json`
- `valid_from` / `valid_to`
- `enabled`

模拟方案、策略模拟和真实交易流水均引用费用配置或保存费用快照。历史交易必须保存实际费用，不能因为未来修改费率而重算历史事实。

## 13. 消息通知体系

通知只负责“何时发送、发到哪里”。通知条件复用规则引擎；通知模块不重新计算 MA、ATR、持仓回撤等指标。

`notification_rules`：

- `notification_rule_id`
- `name`
- `scope`: `SECURITY` / `WATCHLIST` / `POSITION` / `STRATEGY` / `INDUSTRY`
- `target_ref_id`（可空；指定持仓/自选/策略等）
- `rule_id`
- `time_window_json`
- `cooldown_seconds`
- `channel_config_id`
- `enabled`
- `created_at` / `updated_at`

`notification_events`：

- `event_id`
- `notification_rule_id`
- `target_type` / `target_id`
- `trigger_value_json`
- `status`: `待发送` / `已发送` / `失败` / `被抑制`
- `triggered_at` / `sent_at`
- `error_summary`

持仓高点回撤通知链路：

```text
实时/最新价格
  -> 查询“持有中”的真实/模拟持仓
  -> 更新 position_runtime_state
  -> Rule Engine 计算 drawdown_from_high >= 3% ?
  -> Notification Event
  -> 现有 notification_outbox
  -> 邮件/后续渠道
```

## 14. 收益与复盘体系

后台最终需要支持三条可比较净值序列：市场基准、策略模拟、个人真实操作。三者必须使用同一日期轴，并明确费用与现金处理口径。

| 序列 | 来源 | 核心含义 |
|---|---|---|
| 大盘/基准收益 | 指数日线 | 同期市场表现 |
| 策略收益 | `simulation_runs` / `simulation_trades` | 严格执行某策略理论表现 |
| 个人真实收益 | `trade_ledger` + `positions` + 市值 | 实际操作后的真实结果 |

要求：

- 支持总收益、年化、最大回撤、超额收益。
- 支持按证券拆解个人收益与策略收益。
- 真实收益必须扣除实际费用。
- 策略与个人比较时，必须记录比较所使用的策略版本与模拟区间。

## 15. 状态机设计原则

只给真正存在生命周期且不同阶段允许动作不同的对象设计状态机。股票、特征、股票集合成员等不做万能状态。

| 对象 | 状态 |
|---|---|
| 模拟任务 | 待运行 -> 运行中 -> 成功/失败/已取消 |
| 策略 | 草稿 -> 已启用 <-> 已停用；草稿/停用 -> 已归档 |
| 策略运行 | 待运行 -> 运行中 -> 成功/部分成功/失败 |
| 策略候选 | 观察中 -> 已确认/已失效/已过期 |
| 持仓 | 持有中 -> 已清仓（以后可扩部分持有） |
| 通知事件 | 待发送 -> 已发送/失败/被抑制 |

禁止业务代码在任意位置直接修改状态字段。必须通过对应 Service 的显式动作，例如 `enable_strategy()`、`expire_candidate()`、`close_position()`，并在服务层校验允许的状态迁移。

## 16. 服务边界与推荐代码结构

```text
domain/
  features.py
  rules.py
  stock_sets.py
  strategies.py
  candidates.py
  simulation.py
  trades.py
  positions.py
  notifications.py

services/
  feature_service.py
  rule_engine.py
  screening_service.py
  simulation_service.py
  strategy_service.py
  strategy_runner.py
  candidate_service.py
  trade_ledger_service.py
  position_service.py
  notification_service.py
  performance_service.py

repositories/
  ... 对应各 SQLite 表 ...

warehouse/
  datasets.py
  feature_builder.py
  feature_store.py

ops/
  strategy_scheduler.py
  simulation_runner.py
```

服务职责：

| 服务 | 职责 |
|---|---|
| FeatureService | 定义/查询特征、触发计算、取特征版本 |
| RuleEngine | 验证规则、执行条件表达式，不处理业务动作 |
| ScreeningService | 用规则对股票范围过滤并形成股票集合/结果 |
| SimulationService | 创建计划、排队、运行、记录交易与事件 |
| StrategyService | 策略版本、启停、状态迁移 |
| StrategyRunner | 一次加载 Feature View，串行执行所有已启用策略 |
| PositionService | 从交易流水维护持仓和持仓运行状态 |
| NotificationService | 根据规则结果生成通知事件并接现有 outbox |
| PerformanceService | 计算策略、个人、基准收益和对比 |

## 17. 调度与运行

继续使用现有轻量调度基础，不引入新的 DAG 平台。数据任务与业务任务分开。

收盘后主链：

```text
Daily Publish
  -> Feature Build（日频，全市场一次）
  -> Strategy Runner（所有启用策略串行）
  -> 更新 Candidate / Match
  -> 生成需要的通知事件
```

盘中轻量链：

```text
腾讯分钟/快照（仅观察+自选+持仓+候选）
  -> 更新 Position Runtime / 必要实时 Feature
  -> 执行 POSITION / WATCHLIST 通知条件
  -> Outbox
```

## 18. 2C2G 性能约束

1. 全市场日频特征只计算一次，落 Feature View 后所有策略共享。
2. 策略 Runner 单进程串行运行，不为策略开独立 worker。
3. 大型回测最大并发 = 1；任务排队。
4. 分钟数据只采集观察/自选/候选/持仓集合，不采全市场分钟。
5. SQLite 开 WAL，关键表建立 `status`、`strategy_id`、`instrument`、`run_id` 等索引；避免长事务。
6. Parquet 日频按月分区，扫描按日期裁剪；不要按股票拆小文件。
7. 策略运行保存 Feature Snapshot 只保存命中时关键字段，不复制全量 Feature View。
8. 模拟运行按日期顺序流式处理，避免一次性将多年全市场所有中间状态常驻内存。

## 19. 数据一致性与审计

- Simulation Run 保存 `data_versions_json`、`feature_versions_json`、规则快照。
- Strategy Version 保存规则快照，不依赖未来被修改的规则定义。
- 真实 Trade Ledger 保存实际费用和来源，不随配置变化。
- Strategy Match 保存命中时特征快照，便于回答“为什么命中”。
- 状态变更记录 `updated_at`，并建议增加通用 `domain_events` / `audit_log` 记录关键动作。

`audit_log`（可选但建议）：

- `audit_id`
- `object_type`
- `object_id`
- `action`
- `before_json`
- `after_json`
- `created_at`
- `actor`

## 20. API / 服务接口建议

| 能力 | 建议接口 |
|---|---|
| 规则 | `create_rule` / `validate_rule` / `evaluate_rule` / `archive_rule` |
| 股票集合 | `create_stock_set` / `add_members` / `remove_members` / `snapshot_members` |
| 模拟 | `create_plan` / `enqueue_run` / `get_run` / `cancel_run` / `replay_events` |
| 策略 | `create_from_simulation` / `create_version` / `enable` / `disable` / `archive` |
| 候选 | `list_active` / `confirm` / `invalidate` / `expire` |
| 交易流水 | `record_trade` / `amend_trade`（需审计） / `list_trades` |
| 持仓 | `list_positions` / `rebuild_from_ledger` / `update_runtime` |
| 通知 | `create_rule` / `enable` / `disable` / `evaluate` / `dispatch` |
| 复盘 | `strategy_curve` / `personal_curve` / `benchmark_curve` / `compare` |

## 21. 分阶段实现顺序

### 阶段 1：统一特征 + 规则引擎

- `feature_definitions`、`rule_definitions`、RuleEngine、规则校验。
- 现有 MA5/MA10 等指标迁入统一注册，不要求一次迁完全部指标。
- 实现 Feature View 日频共享计算。

### 阶段 2：股票集合 + 模拟闭环

- `stock_sets` / `stock_set_members`。
- `simulation_plans` / `simulation_runs` / `simulation_trades` / `simulation_events`。
- 单回测并发队列、交易成本、基准对比。
- 研究阶段可使用临时规则，不强制创建策略。

### 阶段 3：策略版本与自动运行

- `strategies` / `strategy_versions` / `strategy_runs` / `strategy_matches`。
- 启用/停用状态机。
- 单 StrategyRunner 串行执行。
- 跨日策略增加 `strategy_candidates`。

### 阶段 4：交易账本 + 持仓

- `trade_ledger`。
- `positions` + `position_runtime_states`。
- 费用配置、K线操作标记所需完整历史数据。

### 阶段 5：统一通知 + 三方复盘

- `notification_rules` / `notification_events` 接现有 outbox。
- 持仓后高/回撤通知。
- 个人 vs 策略 vs 大盘收益曲线及差异分析。

## 22. 第一版不要做的事情

- 不要接真实券商自动下单。
- 不要引入 Kafka、Celery 集群、Airflow、Spark。
- 不要为所有业务对象设计统一万能状态。
- 不要让通知、模拟、策略各自实现技术指标。
- 不要把“观察”落成另一份股票主数据。
- 不要把每次策略运行产生的所有 Feature 全量复制到 SQLite。
- 不要把自选、候选、持仓写成 `security.status`。

## 23. 最小完整验收案例

1. 底层 daily 和行业数据已 Publish，Feature Builder 生成 MA、RS、板块回流、距离压力区等。
2. 创建一个规则：板块回流 AND RS改善 AND 距压力区 < 4%。
3. 执行筛选，选择 5 只股票保存为一个股票集合。
4. 创建模拟方案，绑定买入/卖出规则、费用、时间区间和基准。
5. 模拟任务排队 -> 运行 -> 成功，生成逐笔交易、事件、净值和超额收益。
6. 将有效模拟保存为策略 V1，状态草稿；人工启用后进入调度。
7. 策略 Runner 运行，生成命中；跨日候选进入“观察中”，后续确认或过期。
8. 真实用户人工买入后录入交易流水，系统形成真实持仓并维护后高、浮盈、回撤。
9. 高点回撤达到通知条件时生成通知事件并投递邮件；不自动卖出。
10. 系统可以生成同期：策略模拟收益、个人真实收益、基准收益三条曲线。

## 24. 最终后台主链

```text
数据 Publish
  -> 统一特征生成
  -> 条件/规则引擎
  -> 研究筛选 -> 股票集合 -> 模拟 -> 验证 -> 策略版本
  -> 自动策略运行
  -> 命中 / 候选 / 信号

持仓/自选通知条件
  -> 规则引擎
  -> 通知事件
  -> Outbox

用户真实操作 -> 交易流水 -> 持仓账本 -> 持仓运行特征 -> 卖出提醒

模拟交易 -> 策略收益
真实流水 -> 个人收益
指数行情 -> 大盘收益
  -> 复盘比较
```

最终要求：前台即使未来重做，后台对象与核心运行链路仍保持稳定；新增特征、规则、候选状态、通知渠道或未来真实券商适配时，不需要推翻数据层、规则引擎、策略版本和交易账本。
