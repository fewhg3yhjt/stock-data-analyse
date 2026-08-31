# StockInvestmentTool Phase 4 设计说明

> 文档性质：项目内部设计文档，供后续会话和开发模型接手使用。
> 更新时间：2026-08-26
> 适用项目：`/opt/stock_data_analyse`
> 生产地址：`https://stock.easyconnect.ltd/`
> 本文不包含密码、密钥或真实通知地址。

## 1. 阶段背景

StockInvestmentTool 已经具备完整的个人投资工作台雏形：个股分析、天级回测、分钟分时、观察池、自选、模拟、持仓、通知和复盘均可以使用；策略编排器和指标中心也已建立。

当前阶段的主要问题不再是“有没有功能”，而是：

1. 页面入口太多且层级扁平，用户无法按投资任务理解系统；
2. 数据采集和任务执行虽然有后端实现，但用户看不到数据是否新鲜、任务是否成功；
3. 日线全量数据、分钟数据和在线快照是不同任务，页面没有解释数据状态；
4. 股票业务通知已经具备 outbox，但系统故障、数据滞后和任务失败还没有形成系统告警闭环；
5. 策略实验、指标中心、策略编排器之间尚未形成“定义 → 验证 → 发布 → 使用”的清晰产品流程；
6. 旧的兼容代码、直接数据源调用和通知管线仍然存在，需要持续收敛。

Phase 4 的目标是把“功能集合”升级为“可运营的投资工作台”。

## 2. 阶段目标

### 2.1 产品目标

用户打开系统后，应该能够在一个入口回答四个问题：

```text
系统正常吗？
数据新鲜吗？
今天市场、观察池和持仓有什么事？
我下一步该做什么？
```

### 2.2 成功标准

- 一级导航控制在 7 个以内；
- 用户可以从工作台进入所有核心流程；
- 观察、自选、模拟形成一条连续工作流；
- 策略实验、指标、方案编排形成一条研究工作流；
- 数据中心可以展示每类数据的最新状态、最近任务和错误原因；
- 任务和通知故障有可见状态；
- 日线、指标、因子、在线快照和分钟数据彼此隔离、状态可解释；
- 所有新功能都有路由测试、业务测试和生产冒烟证据；
- 不改变既有天级回测的业务语义，不影响现有 `daily` 全量数据。

## 3. 当前系统边界

### 3.1 当前已存在的主要数据集

```text
output/data/warehouse/
├── daily/          全市场天级行情，按月 Parquet
├── indicators/     天级指标分区
├── factors/        因子宽表
├── fundamentals/  单标的基本面历史
├── online/         观察池低频在线快照
└── minute/         观察池腾讯分钟数据，按交易日保存
```

其他持久化：

```text
output/data/portfolio.db              持仓、交易、现金、建议
output/data/notification_outbox.db   通知 outbox、重试、死信
output/data/job_runs.db              定时任务执行台账
schemes/indicators.yaml               内置指标定义
schemes/custom/indicators.yaml        用户指标定义
schemes/*.yaml                        内置策略方案
schemes/custom/*.yaml                 用户策略方案
notifier/notify_rules.yaml            用户通知触发器
```

### 3.2 当前已有能力

- `RuleRegistry`：规则注册、schema、部分生产派发；
- `IndicatorContext`：按名称和表达式取指标，支撑策略和 Advisor 已接入；
- `DataSource`：日线和分钟读取接口，部分业务层已接入；
- `PositionStateMachine`：PortfolioManager、Advisor 和 V6 部分状态字段已接入；
- `NotificationOutbox`：统一 Digest 入队、重试和 dead 状态；
- `JobRunStore`：每日任务、快照和 outbox 重试执行记录；
- `base.html/base.css`：全部业务模板已完成公共骨架迁移；
- `scripts/backup_data.py`：只复制、不删除的备份工具；
- `.github/workflows/test.yml`：Python 3.11 测试和 Docker build 门禁。

### 3.3 明确不属于本阶段自动执行的操作

以下属于生产安全运维动作，不由后续模型自动执行：

- 生产密码轮换；
- `SECRET_KEY` 轮换；
- `.env` 和生产数据库权限变更；
- 加密备份上传；
- 覆盖生产目录的恢复演练；
- 删除历史 outbox dead/pending 记录。

后续代码可以提供检查、提示和操作入口，但执行这些动作前必须说明影响并等待明确确认。

## 4. 产品信息架构

### 4.1 一级导航

目标导航：

```text
工作台
市场
观察池
持仓
研究
复盘
系统
```

一级导航不再直接展示 12 个技术/业务模块。

### 4.2 二级结构

#### 工作台

- 今日概览；
- 今日持仓提醒；
- 今日观察池变化；
- 数据和任务状态摘要；
- 快捷分析、同步、查看持仓。

#### 市场

- 指数；
- 板块；
- 市场状态；
- 市场资金流；
- 个股行情入口。

#### 观察池

合并当前的观察、自选和模拟：

- 全部；
- 手动加入；
- 策略选入；
- 已设模拟入场；
- 持仓同步；
- 观察详情；
- 设置观察起点；
- 设置模拟入场；
- 进入建仓。

这些不是三个孤立系统，而是同一条状态流：

```text
候选
  → 观察
  → 自选
  → 模拟
  → 建仓
```

#### 持仓

- 账户总览；
- 持仓；
- 持仓详情；
- 加仓、减仓、分红、平仓；
- 当前建议和风险线。

#### 研究

研究页面内部使用 Tab 或二级入口：

- 个股分析；
- 策略实验；
- 指标中心；
- 策略编排；
- 方案版本和发布。

研究闭环：

```text
指标定义
  → 策略组装
  → 表达式验证
  → 样本回测
  → 方案发布
  → 个股分析/持仓使用
```

#### 复盘

- 交易流水；
- FIFO 盈亏；
- 胜率和盈亏比；
- 持仓周期；
- 交易和策略版本关联；
- 复盘备注和导出。

#### 系统

系统内部使用 Tab：

- 数据中心；
- 通知中心；
- 账户和系统设置；
- 任务执行台账。

## 5. 工作台设计

### 5.1 页面布局

```text
工作台
├── 顶部：日期、数据状态总灯、刷新时间
├── 今日市场
│   ├── 市场状态
│   ├── 指数涨跌
│   └── 资金流摘要
├── 今日操作
│   ├── 止损/清仓
│   ├── 止盈/减仓
│   ├── 加仓
│   └── 观察池新增候选
├── 数据健康
│   ├── 日线最新交易日
│   ├── 分钟最新时间
│   ├── 最近一次同步
│   └── 待处理通知
└── 快捷操作
    ├── 分析股票
    ├── 查看观察池
    ├── 查看持仓
    ├── 增量同步
    └── 通知台账
```

### 5.2 首页行为

当前首页 `/` 是个股分析页。迁移时必须保留个股分析能力，推荐两种实现之一：

1. `/` 改为工作台，个股分析移动到 `/analyze`；
2. `/` 保留分析页，在顶部增加工作台摘要并提供明确入口。

优先选择方案 1，但必须先确认所有现有链接、表单和生产用户习惯，再拆路由。

### 5.3 工作台不负责交易执行

工作台只展示建议和操作入口。任何真实持仓变更仍然必须进入现有持仓操作流程，不能在概览卡片中绕过事务服务直接修改数据库。

## 6. 数据中心设计

### 6.1 数据状态模型

每类数据统一返回：

```text
dataset
latest_value
latest_trade_date
status
last_success_at
last_failure_at
last_error
rows
symbols
job_name
source
```

状态枚举：

```text
healthy       正常
stale         轻微滞后
critical      严重滞后
running       采集中
failed        最近失败
disabled      未开启
empty         没有数据
unknown       无法判断
```

### 6.2 交易日判断

不能简单用自然日判断 daily 是否滞后。应提供交易日判断器：

```text
latest_expected_trade_day()
classify_freshness(dataset_latest, expected_trade_day)
```

初期可以使用最近日线仓库日期和工作日规则，后续再接正式交易日历。

规则：

- 数据日期 >= 预期交易日：`healthy`；
- 落后 1 个交易日：`stale`；
- 落后 2 个或更多交易日：`critical`；
- 任务未开启：`disabled`；
- 最近任务失败：`failed`。

### 6.3 数据集状态

| 数据集 | 来源 | 预期状态规则 |
|---|---|---|
| daily | 全市场天级 Parquet | 按最新交易日比较 |
| indicators | 指标 Parquet | 不应晚于 daily |
| factors | 因子 Parquet | 不应晚于 daily |
| fundamentals | 单股基本面 | 按股票/报告期展示 |
| online | 10 分钟快照 | 交易时段内检查最新时间 |
| minute | 腾讯分钟 | 交易时段内 15 分钟未更新即告警 |

### 6.4 数据中心页面

顶部显示总状态，下面按数据集显示卡片：

```text
日线 daily       2026-08-21  严重滞后  [查看任务] [立即同步]
指标 indicators  2026-08-21  严重滞后  [重建指标]
因子 factors     2026-08-21  严重滞后  [重建因子]
分钟 minute      2026-08-26 14:52 正常 [查看采集]
在线 online      2026-08-26 22:50 非交易时段 [查看快照]
```

必须解释原因，例如：

```text
日线数据已滞后 3 个交易日。
原因：WAREHOUSE_DAILY_SYNC 未开启。
```

或：

```text
日线数据已滞后 2 个交易日。
原因：最近一次 daily_sync 失败：数据源超时。
```

### 6.5 数据中心操作

- 检查数据状态；
- 增量同步 daily；
- 重建 indicators；
- 重建 factors；
- 手动采集 minute；
- 清理过期 minute/online；
- 查看任务历史。

所有操作必须返回 job id 或运行记录 id，页面显示 running/success/failed，不允许只显示“已启动”后没有查询方式。

## 7. 通知中心设计

### 7.1 三个区域

```text
业务通知
├── 持仓操作建议
├── 价格变化
├── 资金流
└── 盘后摘要

系统告警
├── daily 数据滞后
├── minute 采集中断
├── scheduler 异常
├── outbox 堵塞
├── dead letter 增加
└── 磁盘/数据库异常

投递中心
├── pending
├── retrying
├── sent
├── failed
└── dead
```

### 7.2 通知状态

每条通知应展示：

```text
id
topic
channel
status
attempts
last_error
created_at
sent_at
```

不要在页面回显 webhook、SMTP 密码或 API key。

### 7.3 死信操作

允许：

- 查看死信原因；
- 单条重试；
- 批量重试；
- 标记忽略。

删除历史记录不属于默认操作，必须单独确认。

## 8. 策略研究中心设计

### 8.1 方案生命周期

```text
draft
  → validated
  → published
  → disabled
```

发布前必须满足：

- YAML 和 schema 校验通过；
- 所有指标存在且表达式可执行；
- 样本行情回测成功；
- 规则 type 具备生产 executor；
- 未发现未知参数或未消费参数；
- 记录验证时间、样本代码和结果摘要。

### 8.2 方案影响范围

发布或修改方案时显示：

- 有多少持仓使用该方案；
- 有多少模拟记录使用该方案；
- 新版本是否只影响新持仓；
- 历史持仓是否继续使用 `scheme_snapshot`；
- 当前默认方案是否会变化。

### 8.3 策略实验室

当前 `/strategy` 是固定条件扫描实验。下一阶段应允许选择已验证方案，但必须区分：

- 规则型方案回测；
- 固定扫描实验；
- V6 专用回测。

不要让用户以为三者使用同一算法。

## 9. 架构设计

### 9.1 推荐模块

```text
web/routes/workbench.py
web/routes/data_center.py
web/routes/notifications.py
web/routes/research.py
web/routes/system.py

ops/freshness.py
ops/job_runs.py
ops/health.py

notifier/outbox.py
notifier/system_alerts.py
notifier/digest.py

core/release.py
core/validation.py
```

### 9.2 依赖方向

```text
route
  → application service
  → domain capability
  → interface/data source
  → persistence/external provider
```

禁止：

- route 直接写 SQLite 业务表；
- UI 逻辑直接决定数据源；
- 通过读取日志文本判断任务状态；
- 通过清理旧文件解决状态不一致；
- 新建一个 facade 但生产调用方继续绕过它。

### 9.3 单进程约束

当前仍是单容器单进程模型。scheduler 锁可以防止同一主机多进程重复启动，但不能替代真正的分布式调度系统。若未来扩容，必须把 scheduler 拆成独立服务或引入共享锁/持久化 job store。

## 10. 非功能要求

- 所有数据状态接口响应时间应可测；
- DataSource 查询不得把全市场分区全部加载到 pandas；
- 页面首屏不得强制加载不需要的 ECharts；
- 日线同步失败不得破坏已有 daily 分区；
- 指标重建失败不得把旧 indicators 目录替换为空目录；
- 任务重复执行必须可识别；
- 通知失败不得静默丢失；
- 生产代码不能依赖测试环境变量绕过业务校验；
- 所有 mutation 继续经过鉴权和同源/CSRF 防护。

## 11. 完成定义

Phase 4 不能用“页面能打开”作为完成标准。每项功能必须具备：

```text
产品流程明确
  + 数据模型明确
  + 生产调用链明确
  + 错误和空状态明确
  + 单元/集成测试
  + 页面关键 DOM 测试
  + 本地冒烟
  + 生产冒烟
  + 文档状态更新
```
