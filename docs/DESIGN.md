# StockInvestmentTool 架构设计文档

> 本文档是本项目的**架构分层与模块设计总纲**，作为开发中的设计基准。
> 新模块/大改动应先在此登记分层与衔接关系，再动手实现。
> `stock_daily` 可信数据链路的批次、版本、质量、发布、统一访问和分阶段验收，以 [可信数据链路设计与实施规范 V1.1](DATA_PIPELINE_V1_DESIGN.md) 为专项设计真源。

## 一、总体架构分层

```
┌────────────────────────────────────────────────────────────┐
│ 应用层 web/            页面 / API / 定时任务 / 管理台           │
│  ├─ app.py             Flask 路由（分析/持仓/观察/策略/设置）    │
│  ├─ scheduler.py       定时任务（盘中/盘后/每日）              │
│  └─ templates/         页面模板（ECharts 交互图表）            │
├────────────────────────────────────────────────────────────┤
│ 业务层 portfolio/      持仓/观察池/建议/通知                  │
│       strategy/       策略决策（买卖/风控/仓位）              │
│       notifier/       消息通知（邮件/飞书/企微）              │
│       fundflow/       资金流分析                             │
│       screener/       A股初筛                               │
├────────────────────────────────────────────────────────────┤
│ 指标层 indicators/    可配置/可组合/可编程的指标体系（本层核心）│
│  ├─ 基础指标（原子）    MA(任意N)/MIN/MAX/涨跌幅/量比...        │
│  ├─ 组合指标（表达式）  0.95*MA20、MIN(MA20,MA240)...          │
│  ├─ 代码指标（注册）    Python 函数动态扩展                    │
│  └─ 存储              warehouse/indicators/ 分区             │
├────────────────────────────────────────────────────────────┤
│ 数据层 warehouse/     全量数据仓库                           │
│  ├─ raw/            贴源层（各接口原始数据，互不覆盖）          │
│  ├─ daily/          加工层（标准OHLCV+PE/PB，单位统一）        │
│  ├─ factors/        历史因子归档（目标并入 indicators，不再新增） │
│  ├─ indicators/     指标层（动态可配，单股分析/决策用）        │
│  ├─ fundamentals/   基本面层（完整财务史：ROE/毛利率/扣非/负债率）│
│  ├─ online/         盘中快照                                │
│  └─ management.db   生产唯一数据管理事实；旧库仅作显式迁移/归档输入 │
└────────────────────────────────────────────────────────────┘
```

## 一.一、任务驱动的数据生产架构

数据层不是由若干独立脚本拼接而成，而是由任务框架统一驱动。任务和数据保持两个不同视角：任务负责执行，数据负责定义和可用性。

```text
任务定义 YAML
  -> 调度配置
  -> 执行请求（scheduled/manual/backfill/retry/shadow）
  -> Task Runner
  -> Task Run
  -> 事件与日志
  -> 数据产物

数据集/指标定义 YAML
  -> SQLite 运行时投影
  -> 数据结果与健康状态
  -> Unified Data Access
  -> 下游消费
```

任务生命周期：

```text
任务配置草稿 -> 校验 -> 生效 -> 调度/手动请求 -> 执行记录 -> 产物与血缘
```

周期是任务属性，不是独立业务对象。任务配置同时区分：

- 执行频率：每交易日、每日、每周、每月、每季度、依赖上游或手动；
- 数据周期类型：交易日、天、月、季度或自定义区间；
- 本次执行区间：`period_start`、`period_end`；
- 时区：统一使用 `Asia/Shanghai`。

任务中心管理任务、配置、周期、执行、日志和产物；数据中心管理数据集/指标定义、统一口径、最新周期、覆盖情况和健康状态。Raw、Candidate、Quality Report 等属于任务技术详情，不作为数据中心首屏分类。

存储职责固定为：

```text
YAML       声明系统应该是什么
SQLite     记录实际发生了什么以及可查询运行状态
Parquet/CSV 保存原始数据、标准数据和计算结果
```

判断抽象是否正确的标准：新增任务、数据集或指标变体时，应优先通过配置、注册和稳定接口接入，而不是在 Web 路由和调度器中增加新的分支流程。

## 〇、模块现状（2026-08 最新）

### 已完成模块
| 模块 | 说明 |
|------|------|
| `indicators/` | 指标体系（基础/组合/代码指标，表达式引擎）|
| `warehouse/indicators_build.py` | 全市场指标批量生成（采集后自动触发）|
| `warehouse/fundamentals_collect.py` | 财务史采集写入 Raw/Published 链路；页面仍有在线 fallback 残留，待生产路径收口 |
| `warehouse/indicators/` | 指标宽表分区（37月，6435只）|
| `warehouse/fundamentals/` | 财务史分区（4551只）|
| `strategy_lab.py` | 策略实验室（扫描+回测+ECharts交互）|
| P2-P4 | 持仓操作按钮/个股折线图/账户历史/输入自动关联 |

### 当前仍待推进
1. 全面梳理剩余业务读取路径，逐步统一走数据访问层；
2. 真实生产主链路正式切换前的新旧长期对账和规模性能基线；
3. 辅助数据集的统一 Builder/Publish/Access 细化和长期运行；
4. 生产级告警、恢复演练和数据留存策略；
5. 任务中心批量补数、取消和更细粒度的版本影响确认。

## 二、数据流与衔接关系

### 数据流转
```
数据源(腾讯/东财/baostock)
  → Raw Batch（来源独立、不可覆盖）
  → Candidate（标准化合并+单位统一）
  → Quality（质量结论）
  → Published stock_daily（当前正式版本）
  → Unified Data Access ── ① 指标/因子统一输入
  → indicators/ 指标层（可配置计算）
   → indicators/ 统一指标层（历史 factors 仅归档）
  → 下游：advisor决策 / 折线图 / 策略扫描 / 邮件
```

### 关键衔接点
1. **指标通过 Unified Data Access 读取 Published stock_daily**，不直接枚举 daily 或读取 raw；历史 factors 不属于新的正式消费入口
2. **指标层与历史因子归档边界**：
   - `indicators/`：统一的指标和研究因子生产、读取与业务消费入口
   - `factors/`：历史兼容文件和归档，目标不再更新；下线前不得新增独立任务、API 或消费者
3. **下游消费**：
   - advisor：策略 yaml 可引用指标表达式（止盈点等）
   - 折线图：前端请求指标序列 → 指标引擎现算/读分区
    - 策略扫描：通过统一 Data Access 读取 indicators；历史 factors 只允许显式归档查询
    - 邮件：操作依据里展示指标值

## 二.一、任务中心与数据中心边界

```text
任务中心：任务定义、配置版本、调度周期、执行请求、运行进度、日志、产物和操作
数据中心：数据集/指标定义、统一口径、最新数据周期、覆盖率、健康状态和关联任务
```

两者通过以下关系关联：

```text
Task Definition -> Task Run -> Artifact -> Dataset/Metric Result
```

任务阶段、任务类型、数据产物、状态和页面用词以 [任务与数据术语](TASK_DATA_GLOSSARY.md) 为准。

## 三、指标体系设计（核心）

### 1. 指标分层
| 类型 | 定义 | 示例 |
|------|------|------|
| **基础指标** | 原子操作，来自 Published stock_daily 或简单计算 | MA(close,20)、MIN(a,b)、涨跌幅 |
| **组合指标** | 用已有指标 + 表达式 | 0.95*MA20、0.95*MIN(MA20,MA240) |
| **代码指标** | Python 函数注册，可作其他指标输入 | 自定义复杂指标 |
| **决策指标** | 结合持仓上下文 | 后高/前高/止盈点（表达式配置）|

### 2. 计算引擎
```
① 通过 Unified Data Access 读取 Published stock_daily（date×code）
② 算基础指标（原子）
③ 按表达式依赖顺序算组合指标
④ 算代码注册指标
⑤ 产出指标宽表（时间序列 + 最新值）
```

### 3. 指标配置（schemes/indicators.yaml）
```yaml
bases:                              # 基础指标
  - {name: MA5,   expr: "MA(close,5)"}
  - {name: MA20,  expr: "MA(close,20)"}
  - {name: MA240, expr: "MA(close,240)"}
composite:                          # 组合指标（引用已有）
  - {name: 止盈线, expr: "0.95*MA20"}
code:                               # 代码注册指标
  - {name: 自定义指标, module: "...", function: "..."}
```

## 四、通知策略设计

```
通知策略（notifier/notify_settings.yaml，管理台可编辑）
├── 盘中：每 N 分钟检测，仅当持仓触发操作建议(止盈/止损/加仓)才发
├── 盘后：每日 HH:MM 发送全部持仓汇总
└── 渠道：email（SMTP）/ 飞书 / 企微（webhook）
```

## 五、部署与运行

- 生产环境：Docker 容器 `stock-invest`，代码挂载（改代码 restart 生效）
- 数据源：warehouse 优先，baostock 兜底
- 详见 `stock-deploy` skill 与 wiki `internal/infra/stock-invest-deploy.md`

## 五.一、已验证的任务驱动数据链路

```text
源数据采集任务
  -> 标准数据构建任务
  -> 数据质量任务
  -> 数据发布任务
  -> Unified Data Access
  -> 技术指标任务 / 研究因子任务
```

任务通过配置、周期、执行请求和 Task Run 管理；数据中心关注数据和指标的定义、统一口径、最新周期、覆盖情况和健康状态。Raw Batch、Candidate、Quality Report、checksum 等属于任务技术详情，不是数据中心的主业务分类。当前该链路已通过临时仓库、持久化样例和真实小批量 Shadow 验证，生产正式切流仍需单独批准。

## 六、模块设计约定

1. **新增数据层模块**：优先落 warehouse 分层，不直接对接外部源
2. **指标计算**：统一走指标引擎，不散落硬编码
3. **展示图表**：统一 ECharts 交互式（`web/static/stock-chart.js`）
4. **通知**：统一走 notifier（管理台可配策略）
5. **破坏性操作**：先确认范围（见 AGENTS.md 规范）
