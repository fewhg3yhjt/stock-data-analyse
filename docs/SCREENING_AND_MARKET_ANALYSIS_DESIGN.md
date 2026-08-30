# 选股与行情分析子模块设计 V1

## 1. 模块定位

本模块负责把已发布的市场数据和统一指标条件转化为可查看、可解释、可继续处理的股票候选。

核心问题是：

```text
当前有哪些股票满足条件？
它们为什么满足？
截至哪一天满足？
走势如何？
下一步是否加入观察池或进入策略模拟？
```

本模块不负责：

- 采集和清洗原始数据；
- 定义指标计算逻辑；
- 在筛选脚本中实现完整买入/卖出策略；
- 修改真实持仓；
- 发送通知。

本模块消费数据模块真实返回的 `DatasetResult.data/context`，通过 `DatasetAccess.load_dataset()` 获取 Published `stock_daily`/`indicators`，再消费策略核心的条件评估协议，向观察池、个股研究和回测/模拟模块输出标准结果。

条件配置使用新定义的 `ConditionSpec`，实际执行必须通过新 `RuleRegistry`；走势图必须通过 `DatasetAccess` 读取 Published Dataset，不由本模块自行选择 DataSource 或在线回退。

## 2. 在产品主流程中的位置

```text
Published Market Data
    ↓
Indicator Provider
    ↓
Condition Evaluator
    ↓
Screen Definition
    ↓
Screen Run
    ↓
Screen Candidate
    ├── 查看走势图
    ├── 查看个股研究
    ├── 加入观察池
    └── 使用候选集创建 SimulationPlan
```

选股是候选生成，不是最终买入决策。若筛选条件包含买入条件，也只表示“符合策略入场前置条件”，最终动作仍由策略核心产生 `StrategyDecision`。

## 3. 第一版范围

### 3.1 必须支持

1. 选择一个已保存的筛选方案。
2. 使用指定历史或最新交易日执行筛选。
3. 支持单个条件和 `AND/OR/NOT` 条件组合。
4. 支持行情字段、指标字段、行业和证券基础属性筛选。
5. 返回命中股票、命中值、条件解释和数据日期。
6. 查看单只股票日线走势。
7. 在走势图上显示指标线和命中信号点。
8. 从结果加入观察池。
9. 从结果创建回测/模拟计划。
10. 保存筛选运行和候选结果，支持历史查看。
11. 结果明确显示数据版本、质量状态和是否陈旧。

### 3.2 暂不支持

- 任意 Python 筛选脚本；
- 盘中实时全市场扫描；
- 自动建仓；
- 在筛选器中直接修改持仓；
- 复杂机器学习排名；
- 不可解释的黑盒综合评分；
- 将筛选结果直接视为买入信号。

## 4. 核心概念

### 4.1 ScreenDefinition

用户或系统保存的一组筛选条件。它是可复用的配置，不是一次执行结果。

建议字段：

```text
screen_id
name
description
asset_types
universe_spec
condition_spec
sort_spec
display_fields
status
version
created_at
updated_at
```

状态：

```text
draft
published
disabled
archived
```

筛选方案版本发布后不可原地修改。修改条件必须创建新版本。

### 4.2 ScreenRun

一次具体筛选执行，必须锁定运行上下文。

建议字段：

```text
run_id
screen_id
screen_version
run_type
requested_as_of
actual_data_as_of
universe_id
universe_fingerprint
dataset_versions
indicator_versions
condition_snapshot
status
matched_count
started_at
finished_at
error
```

`run_type` 第一版支持：

```text
latest
historical
manual
scheduled
```

### 4.3 ScreenCandidate

一次运行中命中的一只股票。不能只保存当前结果数量，必须保存候选明细。

建议字段：

```text
candidate_id
run_id
symbol
name
asset_type
industry
rank
score
matched
condition_results
display_values
data_as_of
expires_at
```

其中：

- `condition_results` 保存每个条件的通过与否、实际值和阈值；
- `display_values` 保存页面展示所需的标准字段；
- `score` 如果没有明确评分模型必须为空，不能用排序位置伪造评分；
- `expires_at` 用于候选有效期，不代表观察对象自动删除。

### 4.4 ChartQuery

走势图查询不是筛选运行，但必须使用同样的数据上下文。

建议输入：

```text
symbol
start_date
end_date
frequency
adjustment
indicators
signal_run_id
```

建议输出：

```text
symbol
bars
indicator_series
signal_points
data_context
```

## 5. 条件模型

条件模型复用策略核心的 `ConditionSpec`，本模块不另外发明一套条件语法。

第一版支持：

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

示例：

```json
{
  "type": "and",
  "conditions": [
    {
      "type": "comparison",
      "left": {"field": "close"},
      "operator": ">",
      "right": {"indicator": "ma60"}
    },
    {
      "type": "cross",
      "left": {"indicator": "ma20"},
      "direction": "above",
      "right": {"indicator": "ma60"}
    }
  ]
}
```

### 5.1 可引用数据

第一版允许引用：

```text
行情字段：open/high/low/close/pre_close/volume/amount/turn
基础属性：symbol/name/industry/asset_type
指标：已注册且已发布的指标
衍生统计：N 日收益、N 日涨停次数、距指标偏离率、历史长度
```

不允许在条件中直接引用：

- 任意数据库列；
- 任意 Parquet 原始列；
- 未登记指标；
- 页面临时计算字段；
- 当前日期之后的数据。

### 5.2 空值语义

条件遇到空值时不能默认当作 `false` 后静默忽略。必须返回：

```text
passed = false
evaluation_status = missing_data
missing_fields
explanation
```

运行层根据配置决定：

```text
missing_data → candidate_not_matched
missing_data → run_partial_success
missing_data → run_failed
```

第一版建议：单只股票缺数据不影响其他股票执行，但 `ScreenRun` 必须记录 `partial_success` 和受影响股票。

### 5.3 时间边界

对历史日期 `as_of = T` 的筛选：

1. 行情只能使用 `date <= T` 的数据。
2. 指标只能使用截至 `T` 计算的值。
3. N 日窗口必须完全落在 `T` 及之前。
4. 未来数据不能用于排序、过滤或解释。
5. 缓存命中后仍必须按请求日期裁剪。

## 6. Universe 模型

筛选运行必须明确扫描哪些证券，不能每次隐式读取当前全量表。

建议字段：

```text
universe_id
universe_type
symbols
symbol_count
fingerprint
as_of
```

第一版支持：

```text
all_active_stocks
selected_symbols
industry_symbols
portfolio_symbols
watchlist_symbols
```

运行时必须保存 `universe_fingerprint`。同一 ScreenDefinition 在不同标的集合上运行，必须产生不同的运行上下文。

停牌或无当日行情证券的处理：

1. 不因单只停牌股票导致整个运行失败。
2. 记录其最近可用数据日期。
3. 不将旧日期数据伪装成目标日期数据。
4. 结果中显示 `symbol_data_as_of` 和 `is_stale`。

## 7. 筛选执行流程

```text
接收 ScreenDefinition + 请求日期
→ 解析并校验条件
→ 固化 Universe
→ 固化 Dataset/Indicator Version
→ 批量加载行情和指标
→ 按 symbol/as_of 构建 StrategyContext，并读取统一 MarketRegime
→ 评估条件树
→ 保存每个条件结果
→ 生成 ScreenCandidate
→ 按明确 SortSpec 排序
→ 保存 ScreenRun 和候选明细
→ 返回结果和 DataContext
```

### 7.0 执行引擎

全市场筛选不得逐股调用 Python 解释器。采用混合执行：

```text
ConditionSpec
→ Screen Compiler
    ├── SQL-capable → DuckDB SQL
    ├── vectorizable → batch/vectorized evaluation
    └── unsupported → bounded exact evaluation
```

第一版条件执行能力：

| 条件 | 执行方式 |
|---|---|
| 字段/指标比较 | DuckDB SQL |
| 指标交叉 | SQL window/LAG |
| 连续 N 日 | SQL window |
| N 日计数 | SQL aggregation/window |
| 行业过滤 | SQL join |
| 复杂登记表达式 | 向量化 |
| 任意 Python | 禁止 |

SQL 阶段只能产生精确命中集的保守超集：

```text
RuleRegistry exact result ⊆ SQL candidate set
```

最终命中只能由 RuleRegistry 精确评估决定。每个条件声明 `compile_mode`：`conservative_sql`、`fully_equivalent_sql` 或 `exact_only`；不满足保守超集约束的条件不得进入 SQL 缩小阶段。测试必须验证固定样本上的集合关系，声明等价的条件还必须验证两者完全相等。

### 7.1 排序规则

排序必须显式配置：

```text
field
direction
nulls
tie_breaker
```

默认建议：

```text
score DESC
return DESC
symbol ASC
```

没有评分时不得隐式按数据库扫描顺序排序。

### 7.2 运行状态

```text
requested
→ running
→ success
→ partial_success
→ failed
→ cancelled
```

`success` 表示所有目标证券均完成评估；`partial_success` 表示存在证券因数据缺失或计算错误未完成；`failed` 表示没有可用结果或运行无法建立上下文。

## 8. 走势图设计

### 8.1 日线图

第一版至少显示：

- K 线；
- 成交量；
- MA20；
- MA60；
- 用户选择的其他已发布指标；
- 筛选命中日期；
- 策略信号点（如果传入 `signal_run_id`）。

### 8.2 图表数据上下文

图表接口必须返回：

```text
data_as_of
requested_start
requested_end
dataset_version
quality_status
source
fallback_used
```

如果实际数据截至日期早于请求结束日期，页面必须显示滞后提示，不得只返回空白或继续显示“最新”。

### 8.3 历史查询

历史查询必须跨越多个分区，不能只读取最新月分区。

接口必须保证：

```text
returned_date >= start_date
returned_date <= end_date
```

ChartService 的唯一数据提供者是：

```text
ChartService → DatasetAccess → Published Dataset
```

研究模式的显式 fallback 必须在 DataContext 标记，正式选股和历史图表禁止静默在线回退。

即使底层缓存或文件包含更长日期范围，也不得把范围外数据返回给调用方。

## 9. 与策略模块的衔接

选股条件与策略条件共用 `ConditionSpec`，但用途不同：

```text
ScreenDefinition
    → 判断哪些股票满足条件

StrategyVersion
    → 判断在当前持仓/现金上下文下应该采取什么动作
```

如果某个筛选方案来源于策略，应保存：

```text
source_strategy_id
source_strategy_version
```

筛选结果不能只保存文本“策略选中”，必须保存具体策略版本和条件快照。

从筛选结果创建模拟时：

```text
ScreenRun
    → SimulationPlan.universe_snapshot
    → SimulationPlan.source_screen_run_id
```

模拟必须能复用当时的候选集合，不能在运行时重新扫描当前 Universe。

## 10. 与个股研究的衔接

从候选进入个股研究时，传递：

```text
symbol
screen_run_id
candidate_id
data_as_of
source_strategy_version
```

个股研究页面应显示：

- 本次候选的来源；
- 命中的条件；
- 命中时的指标值；
- 后续研究使用的数据是否已经更新；
- 当前研究结论是否仍基于原始候选日期。

## 11. 与观察池的衔接

加入观察池不是简单写入一个代码。至少传递：

```text
symbol
source_type = screen
source_run_id = screen_run_id
source_candidate_id = candidate_id
source_strategy_id
source_strategy_version
discovered_at
reason_snapshot
data_as_of
```

如果同一股票已经存在观察记录：

1. 不覆盖原始来源。
2. 新增来源关系或候选事件。
3. 更新最新命中信息。
4. 保留历史筛选记录。

## 12. 与现有能力的对应关系

| 当前能力 | 目标归属 | 处理方式 |
|---|---|---|
| 新 `ScreenExecutor` | ScreenRun Store | 直接实现统一筛选执行 |
| 新 `ConditionCompiler` | RuleRegistry | 直接编译统一条件 |
| `DatasetAccess` | Published Data Access | 所有正式筛选必须经过 |
| `IndicatorContext` | Evaluation Context | 统一指标读取 |
| `market.html` | Market View | 展示市场和板块上下文 |
| `market_discovery.html` | Screening View | 展示运行、候选和下一步动作 |
| `strategy.html` | Screen/Study View | 明确它是信号研究还是正式筛选 |

## 13. API 目标

### 13.1 创建筛选方案

```text
POST /api/screens
```

请求：

```json
{
  "name": "趋势候选",
  "description": "价格站上 MA60 且 MA20 上穿 MA60",
  "universe": {"type": "all_active_stocks"},
  "conditions": {"type": "and", "conditions": []},
  "sort": {"field": "score", "direction": "desc"}
}
```

返回：

```json
{
  "screen_id": "...",
  "version": 1,
  "status": "draft",
  "validation": {}
}
```

### 13.2 执行筛选

```text
POST /api/screens/{screen_id}/runs
```

请求：

```json
{
  "version": 1,
  "as_of": "YYYY-MM-DD",
  "universe": {"type": "all_active_stocks"}
}
```

返回必须包含：

```text
run_id
status
matched_count
data_context
status_url
```

### 13.3 查询运行结果

```text
GET /api/screen-runs/{run_id}
GET /api/screen-runs/{run_id}/candidates
```

### 13.4 查询走势图

```text
GET /api/market/stocks/{symbol}/chart
```

参数：

```text
start
end
indicators
screen_run_id
```

### 13.5 加入观察池

```text
POST /api/screen-candidates/{candidate_id}/observe
```

后端必须从 Candidate 记录读取来源和上下文，不能只接受前端传入的裸股票代码。

### 13.6 创建模拟计划

```text
POST /api/screen-runs/{run_id}/simulation-plan
```

必须使用该 Run 的候选集合快照，允许用户修改但需要生成新的 Universe Snapshot。

## 14. 错误和状态语义

统一错误类型：

```text
SCREEN_NOT_FOUND
SCREEN_VERSION_NOT_FOUND
SCREEN_INVALID
UNIVERSE_INVALID
DATA_CONTEXT_UNAVAILABLE
DATA_STALE
INDICATOR_UNAVAILABLE
SCREEN_CONFLICT
SCREEN_RUN_NOT_FOUND
SCREEN_RUN_FAILED
```

筛选 API 不应把以下情况返回为空列表：

- 条件字段不存在；
- 指标未发布；
- 数据版本不可用；
- 查询区间非法；
- Universe 为空；
- 运行失败。

空列表只能表示：筛选执行成功，但没有股票满足条件。

## 15. 实施步骤

### Step 1：抽取统一筛选契约

实现：

1. `ScreenDefinition`。
2. `ScreenRun`。
3. `ScreenCandidate`。
4. `ConditionSpec` 复用。
5. `ScreenValidator`。
6. `ScreenResult`。

先用当前市场发现的一组条件跑通，不迁移所有旧条件。

### Step 2：实现单次运行和候选存储

要求：

1. 固化 Universe。
2. 固化数据版本和指标版本。
3. 保存每个条件结果。
4. 保存实际数据日期。
5. 支持历史运行查询。

### Step 3：实现走势图查询

要求：

1. 跨分区读取。
2. 严格按日期裁剪。
3. 返回 DataContext。
4. 支持指标线和筛选信号点。

### Step 4：接入观察池和模拟

要求：

1. 从 Candidate 进入观察池时保留来源关系。
2. 从 ScreenRun 创建 SimulationPlan 时固化候选集合。
3. 不直接创建真实持仓。

### Step 5：迁移旧入口

1. 让 `market_discovery` 调用统一 Screen Executor。
2. 将 `strategy_lab` 的固定条件包装为预置 ScreenDefinition。
3. 旧 API 直接下线，不保留兼容路径。

## 16. 测试要求

### 条件和运行

1. AND/OR/NOT 逻辑正确。
2. 交叉条件不读取未来数据。
3. N 日窗口边界正确。
4. 空值返回 `missing_data`，不静默通过。
5. 同一输入重复运行结果一致。
6. Universe 变化会形成不同 fingerprint。

### 候选和追溯

1. 每个候选都能追溯到 ScreenRun。
2. 每个候选保存命中条件、实际值和数据日期。
3. 加入观察池保留 ScreenRun 和 Candidate 来源。
4. 创建模拟使用运行时候选快照，不重新扫描当前市场。

### 图表和日期

1. 历史查询跨月可用。
2. 返回数据严格位于请求区间内。
3. 缓存包含未来数据时不会返回未来数据。
4. 数据滞后时返回明确 `is_stale`。
5. 指标缺失时返回明确错误或缺失状态。

### API

1. 无效条件返回 4xx 和结构化错误。
2. 执行成功但无命中返回空候选，而不是运行失败。
3. 运行失败可查询失败原因。
4. 长任务返回 `run_id` 和 `status_url`。
5. 前端传入裸 symbol 不能伪造候选来源。

## 17. 验收标准

给定已发布日线数据和以下筛选条件：

```text
close > ma60
且 ma20 上穿 ma60
且近 10 日涨停次数 >= 1
```

用户能够：

1. 保存筛选方案。
2. 在指定交易日执行筛选。
3. 得到候选列表和每项命中解释。
4. 查看候选股票的 K 线、MA20、MA60 和信号点。
5. 查看数据截至日期和数据版本。
6. 将候选加入观察池且保留来源。
7. 使用候选集合创建 SimulationPlan。
8. 重复执行同一输入得到一致候选结果。
9. 查询历史运行并复现当时的候选集合。

## 18. 后续依赖

本模块完成后，后续模块依赖：

```text
观察池       ← ScreenCandidate / ScreenRun
个股研究     ← symbol / candidate context
策略模拟     ← universe snapshot / source screen run
持仓复盘     ← source screen / strategy version
通知         ← screen run completed / candidate event
```

任何新筛选功能必须优先扩展 `ConditionSpec` 或注册新指标，不得新增一个只服务于某个页面的独立 SQL 条件系统。
