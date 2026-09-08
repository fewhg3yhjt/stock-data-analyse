# 个股研究与分析子模块设计 V1

## 1. 模块定位

本模块负责对单只股票或 Observation 进行结构化研究。输入必须来自 `DatasetResult.data/context`，形成可复现、可追溯、可供策略和复盘使用的研究结果。

```text
ScreenCandidate / Observation
→ ResearchRun
→ ResearchEvidence
→ StrategyDecision
→ Simulation / Advice / Review
```

研究不是一次性页面拼装，也不是回测本身。研究负责形成判断和计划，回测/模拟负责验证策略在历史交易过程中的表现。

固定策略只固化条件、指标依赖、规则优先级和执行规则，不隐含本次运行的日期范围。研究运行必须显式指定自己的 `data_as_of` 和历史数据范围；不能因为来源筛选在一个月窗口内执行，就自动把研究范围或研究基准日解释为该窗口。

## 2. 第一版范围

必须支持：

1. 从股票代码、筛选候选或 Observation 发起研究。
2. 锁定数据版本、指标版本、策略版本和 `data_as_of`。
3. 读取技术数据和市场上下文；估值数据以降级方式读取；基本面研究依赖 `fundamentals` 契约完成后再正式启用。
4. 使用 `IndicatorContext` 读取指标。
5. 使用 `RuleRegistry` 评估规则。
6. 生成结构化研究结论。
7. 生成标准 `StrategyDecision`。
8. 保存研究证据和值快照。
9. 生成可选 Markdown/HTML 报告。
10. 支持后续创建 SimulationPlan、ObservationSnapshot 和 Advice。

能力状态以 `DATA_PIPELINE_V1_DESIGN.md` 的数据依赖矩阵为准：

```text
技术研究：可用，第一版正式验收
估值研究：降级，字段或标的缺失必须明确展示
基本面研究：受限，基础数据和采集能力存在，但 schema、报告期/公告日契约、Published-only 消费和生产验收仍未完成
市场状态：可用，基于 stock_daily/indicators 生成统一 MarketRegime
```

## 3. ResearchRun

### 3.1 输入

```text
research_run_id
subject_type
symbol / symbols
source_screen_run_id
source_candidate_id
observation_id
strategy_id
strategy_version
data_context
indicator_versions
requested_start
requested_as_of
research_anchor
research_config
```

当研究从筛选候选发起时，`research_anchor` 必须明确：

```text
last_match       默认使用候选最近一次命中日期
first_match      回溯首次命中日期
current          使用当前最新可用数据日期
custom           调用方显式指定日期
```

候选的 `representative_signal_date` 只能作为默认锚点，不能覆盖研究自己的数据上下文。研究保存后必须固化实际 `data_as_of`、数据版本和指标版本。

第一版 `subject_type`：

```text
single_symbol
observation
```

### 3.2 输出

```text
research_run_id
status
technical_assessment
valuation_assessment
fundamental_assessment
market_assessment
entry_plan
exit_plan
strategy_decision_ids
evidence_ids
report_id
warnings
error
started_at
finished_at
```

### 3.3 状态

```text
requested
→ running
→ success
→ partial_success
→ failed
→ cancelled
```

`partial_success` 表示核心研究部分完成但某一非核心能力不可用，例如基本面缺失或 LLM 报告失败。

## 4. ResearchEvidence

### 4.1 字段

```text
evidence_id
research_run_id
evidence_type
source
metric_name
actual_value
threshold_value
assessment
explanation
data_as_of
input_snapshot
```

### 4.2 类型

```text
technical
fundamental
valuation
market
strategy_rule
risk
```

## 5. 研究执行流程

```text
创建 ResearchRun
→ 固化 DataContext
→ 读取已发布行情/基本面/指标
→ 构建 StrategyContext
→ 技术评估
→ 基本面评估（数据契约完成后启用，否则记录 deferred）
→ 估值评估（受限数据，记录 unavailable/partial）
→ MarketRegimeService 读取/生成统一市场状态
→ RuleRegistry 评估策略条件
→ 生成 StrategyDecision
→ 保存 ResearchEvidence
→ 生成报告
→ 完成 ResearchRun
```

LLM 只属于报告生成阶段：

```text
核心研究成功 + LLM 失败 = partial_success
```

LLM 不得重新计算或覆盖结构化决策。

## 6. 新系统组件职责

| 当前能力 | 目标归属 |
|---|---|
| `ResearchService` | Research application service |
| `ResearchResult` | ResearchRun 输出和 Evidence |
| `datasource` | 仅通过统一 DataContext 使用 |
| `IndicatorContext` | 指标读取和表达式上下文 |
| `RuleRegistry` | 条件和规则评估 |
| `FundamentalAssessment` | 基本面评估 |
| `MarketAssessment` | 唯一市场状态评估 |
| `ResearchReportBuilder` | 报告生成 |

`ResearchService` 不得自行读取文件、网络或实现第二套规则执行。

## 7. API 目标

```text
POST /api/research-runs
GET  /api/research-runs/{run_id}
GET  /api/research-runs/{run_id}/evidence
GET  /api/research-runs/{run_id}/decision
GET  /api/research-runs/{run_id}/report
POST /api/research-runs/{run_id}/simulation-plan
POST /api/research-runs/{run_id}/observation-snapshot
```

创建研究时必须支持：

```text
screen_run_id
candidate_id
observation_id
research_anchor
requested_start
requested_as_of
```

## 8. 验收标准

给定一个候选股票，系统必须能够：

1. 保存可查询的 ResearchRun。
2. 显示技术和市场判断；估值按降级标准展示；基本面在数据契约完成前显示延期状态。
3. 显示每个判断使用的实际值和数据日期。
4. 显示策略版本和最终 StrategyDecision。
5. 从研究结果创建模拟计划。
6. 从研究结果保存观察快照。
7. LLM 失败不影响核心结构化研究。
8. 历史研究结果不因再次研究而被覆盖。
9. 从区间筛选候选发起研究时，研究必须明确使用首次命中、最近命中、当前数据或自定义日期之一，不能隐式继承整个筛选窗口。

第一版不以完整基本面研究作为通过条件；不得用旧文件、未公告数据或在线 fallback 伪造基本面完成。估值能力按降级标准验收。

---

## 实现状态与记录

### 实现状态：P0 核心完成（ResearchService）

### 已完成交付物

| 文件 | 能力 | 测试 |
|---|---|---|
| `biz/research.py` | ResearchService：ResearchRun/ResearchEvidence/ResearchResult + 技术/市场/估值/基本面四类评估 + 策略决策生成 + 证据收集 | `tests/test_biz_research.py`（5） |

### 实现要点

1. **数据来源**：ResearchService 接收已通过 `load_dataset` 得到的 `df + context`，不自行读数据文件。
2. **能力状态与数据契约对齐**：技术研究=可用；估值=降级（`valuation_daily` 受限，有 pe_ttm/pb_mrq 列才评估，否则 unavailable）；基本面=limited（`fundamentals` 有基础数据和 Published 产物，但字段契约、生产消费和验收仍受限）。
3. **LLM 边界**：LLM 仅属报告生成阶段，本服务不含 LLM 调用，结构化决策不依赖 LLM。
4. **决策输出**：result.decisions 保存 StrategyDecision 对象引用，供调用方持久化。

### 开发中遇到的问题与决策

1. **决策对象与 id 分离**：初版 result 只存 strategy_decision_ids，无法直接落库决策。决策：增加 `decisions` 对象列表字段，repository 由调用方 save_decision。
2. **空数据降级**：空 DataFrame 返回 technical=unavailable，不抛异常。

### 后续待开发

- ResearchReport 生成（Markdown/HTML）
- 基本面评估接入（`fundamentals` 契约完成后）
- ResearchRun 持久化关联 Observation/SimulationPlan

### 已补充

- `web/biz_api.py` 已提供 `/api/biz/research-runs`，通过 DatasetAccess 加载明确日期范围的数据并持久化 ResearchRun、ResearchEvidence 和 StrategyDecision。
- 已补齐正式 `/api/research-runs` 的 evidence、decision、report 和 observation-snapshot 查询/创建入口。
- 研究 API 通过 `biz/data_access.py` 同时加载 Published `stock_daily`/`indicators`，保留各自数据上下文。
- ResearchEvidence 已保存标的、数据日期、数据上下文、市场状态、实际输入字段和评估结果快照。
- 研究 API 已改为持久化 BusinessRequest/JobRun 后返回 202；研究页面轮询业务运行状态，完成后查询 ResearchRun。
- `ResearchService` 生成的 Evidence/Decision 对象已由调用方统一持久化，避免只保存 ID 而丢失实际对象。

### 跨模块验证

- `tests/test_biz_api.py` 和 `tests/test_biz_end_to_end.py` 已验证研究运行可关联筛选来源并向 Observation/Simulation 传递上下文。
- 当前报告为结构化结果的 Markdown 渲染，LLM 报告适配器仍待接入；从研究结果创建 SimulationPlan 的专用入口已补齐基础实现。
