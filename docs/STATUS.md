# StockInvestmentTool 整体现状与架构梳理

> 本文档为项目现状盘点：架构分层、模块实现、数据流、以及**已确认的问题清单**（架构层面 + 产品/用户视角）。
> 用途：作为后续「怎么做」改造讨论的工作底稿。定稿后可与 DESIGN.md 合并。

更新日期：2026-08-31（持续落地 SRD FR-1 ~ FR-5；真实完成度以本文审计结论和 `docs/IMPLEMENTATION_GUIDE.md` 为准）

文档性质：当前实现盘点，不定义目标契约。文中“已完成”仅表示代码或测试已存在，不等于已接入生产或已完成切换验收。

---

## 一、项目定位

面向个人使用的 A 股投研一体化工具，覆盖投资决策完整闭环：

```
数据获取 → 指标/估值计算 → 策略买点计划 → 历史回测 → 持仓跟踪 → 建议/提醒 → 复盘
```

两条核心设计主张：

1. **配置驱动策略**（`schemes/*.yaml`）：买卖点/风控/回测参数外置，代码只提供原子执行能力。
2. **离线全量 + 在线聚焦**（`warehouse`）：全市场 6435 只 × 3 年日线落 Parquet，观察池/持仓只读本地。

---

## 二、架构分层

```
┌ 应用层    web/            Flask 蓝图(单文件约 3k 行) + APScheduler 定时任务 + Jinja 模板 + ECharts
├ 编排层    core/engine.py  统一分析管线（CLI 与 Web 共用），7 步流水线
│           core/registry.py 方案注册中心（YAML 扫描/加载/缓存/热更新）
│           core/scheme.py   YAML → SchemeConfig 数据模型
├ 业务层    portfolio/      持仓管理(manager 796行) + 买后顾问(advisor 513行) + 三页看板(dashboard 1026行)
│           strategy/       买卖决策（v4.5 与 V6.0 两代并行）
│           backtest/       回测引擎（v4.5 委托 TakeProfitOptimizer；V6.0 机械状态机）
│           notifier/ fundflow/ screener/  独立子工具
├ 指标层    indicators/      表达式引擎（基础/组合/代码指标）
├ 数据层    warehouse/       Parquet 月分区 + management.db（生产）/meta.db（旧回退）+ DuckDB 扫描
│           datasource/      baostock(长连接+自愈) / AkShare / 腾讯行情
└ 前端      web/static/      多个业务 JS + 多个 Jinja 模板；精确数量以自动检查为准
```

---

## 三、核心数据流（理解全库的钥匙）

存在**两条并行取数路径**，这是当前架构最重要、也最需要收敛的一点：

- **路径 A — 在线实时（老）**：`StockDataFetcher`（`datasource/fetcher.py`）→ baostock/AkShare，带 CSV/JSON 缓存 + 连接自愈（monkeypatch send_msg 死循环 + signal 看门狗 + 重连退避）。
- **路径 B — 离线仓库（新）**：`Warehouse`（`warehouse/storage.py`）→ Parquet 月分区 + DuckDB 按需读。

桥接点在 `portfolio/monitor.py` 的 `PriceMonitor.fetch_kline`：**warehouse 优先，baostock 兜底**。但该「优先/兜底」逻辑以 if-else 散落在 `monitor.py`、`core/engine.py`、`web/app.py` 多处，没有统一的 `DataSource` 抽象。

---

## 四、模块清单与规模

| 模块 | 文件 | 行数 | 职责 |
|------|------|------|------|
| 入口 | `main.py` | 458 | CLI 统一入口（分析/对比/持仓/自选/晨报） |
| Web | `web/app.py` | 约 3097 | **单文件** Flask 入口，路由和部分应用编排仍高度集中 |
| 编排 | `core/engine.py` | 421 | 7 步分析管线（CLI/Web 共用） |
| 方案 | `core/scheme.py` + `registry.py` | 212+174 | YAML → SchemeConfig，注册中心 |
| 数据 | `datasource/fetcher.py` | 980 | baostock/AkShare 拉取 + 连接自愈 |
| 指标 | `datasource/indicators.py` | 316 | 技术指标 + 估值（PE 分位/三重锚） |
| 策略 | `strategy/take_profit.py` | 910 | v4.5 止盈优化器 + 网格搜索 |
| 策略 | `strategy/buy_tree.py` 等 | — | V6.0 纯函数决策树 |
| 回测 | `backtest/engine.py` + `engine_v6.py` | 114+459 | 两代回测引擎 |
| 持仓 | `portfolio/manager.py` | 796 | 建仓/交易/平仓/纠错/删除 |
| 决策 | `portfolio/advisor.py` | 513 | 买后顾问（止损>止盈>加仓优先级链） |
| 看板 | `portfolio/dashboard.py` | 1026 | 三页看板数据聚合 |
| 存储 | `portfolio/storage.py` | 596 | SQLite 单文件 + 轻量迁移 |
| 指标 | `indicators/engine.py` | 254 | 表达式引擎（基础/组合/代码指标） |
| 仓库 | `warehouse/storage.py` | 约 390 | Parquet 月分区 + 生产 management.db / 旧 meta.db 回退 |
| 通知 | `notifier/notify.py` + `web/scheduler.py` | 391+469 | 消息构造 + 定时调度 |
| 前端 | `web/static/*.js` | 当前多个文件 | 具体文件和数量以自动检查为准 |

---

## 五、设计亮点（值得保留）

1. **配置驱动策略抽象正确**：`type + params` 声明式 + `strategy_spec` 纯文档段分离，把「人能读懂的说明书」与「机器执行的参数」分开。
2. **数据层分层正在收口**：`raw/daily/indicators/fundamentals/online` 是目标正式链路；`factors` 已确定废弃但历史文件、术语和兼容入口仍残留（见 DESIGN.md 和指标归一 backlog）。
3. **工程化细节扎实**：baostock 连接自愈（死循环补丁/看门狗/退避）是真实踩坑产物。
4. **回测双引擎同口径对齐** + 状态机不变量断言，有防回归意识。
5. **知识沉淀**：DESIGN.md / HANDOVER.md 的会话交接习惯，优于多数个人项目。

---

## 六、问题清单

### 6.1 架构层面（技术债）

| # | 问题 | 位置 | 影响 |
|---|------|------|------|
| A1 | **两代策略引擎「假并行」，实际两套不互通分叉** | v4.5 规则硬编码在 `multi_buy.py`/`take_profit.py` 靠 `find_buy_rule("support_level")` 写死 type 名读取；仅 V6.0 用 `v6_dispatch.py` 注册表 | 「配置驱动」只对已知 6-7 个 type 成立，**新增 rule type 仍要改代码**，与 README「新增方案无需改代码」不符 |
| A2 | **支撑位算法三处复制粘贴** | `multi_buy.py:109`、`take_profit.py:451`、`engine_v6.py:135` | 靠注释保证「同口径」，人肉维护，改一处漏两处=隐性回测偏差 |
| A3 | **`web/app.py` 约 3k 行单文件** | `web/app.py` | 路由平铺，部分旧入口仍同步执行或自行编排，难维护 |
| A4 | **数据源无统一抽象** | `monitor.py`/`engine.py`/`app.py` 散落 if-else | 无法整体切换/测试，DESIGN.md 待办自述「全面梳理数据源统一走数据层」 |
| A5 | **指标层「半接入」** | `indicators/engine.py` 只在 `advisor.py:127` 被 `latest()` 调用一次 | 表达式引擎建好但未被策略消费，与 `datasource/indicators.py` 功能重叠 |
| A6 | **持仓状态机跨模块隐式推进** | `manager.py`/`advisor.py`/`take_profit.py`/`engine_v6.py` 各自推进 `position_phase` | 回测与实盘两套独立状态机，靠注释对齐 |
| A7 | **回测与实盘口径漂移风险** | 回测 `year_high` 滚动 252 日窗口 vs 实盘 `tail(252).max()` | 新股/短数据期处理可能不一致 |

### 6.2 数据与性能（用户体感「页面打开很慢」）

| # | 问题 | 位置 | 根因 |
|---|------|------|------|
| P1 | **个股图表逐月循环读 Parquet** | `dashboard.py` 的 `stock_chart_series`/`stock_dual_view` | 用 pandas `read_parquet` 逐月读 + `df[df.code==...]` 过滤；对比 `PriceMonitor._fetch_from_warehouse` 用 DuckDB 单查询，明显不一致 |
| P2 | **echarts.min.js 1MB 未拆分/未懒加载** | `web/static/echarts.min.js` | 每页全量加载 1MB |
| P3 | **观察池每次打开拉腾讯实时** | `dashboard.py:140 _refresh_prices` | 即使有当日 online 快照也实时拉取 |
| P4 | **持仓页每只持仓可能走 AkShare 网络** | `dashboard.py:866 _fundamental_snapshot` | 财务史无缓存时实时拉取 |

### 6.3 数据引擎切换残留

| # | 问题 | 当前事实 | 影响 |
|---|---|---|---|
| M1 | **旧 `warehouse/meta.db` 尚未下线** | 生产环境通过 `MANAGEMENT_DB_PATH` 已将 Warehouse 元数据路径指向 `management.db`；但默认回退、行业/标的旧读取、测试和迁移脚本仍引用 `meta.db` | 现在直接删除会破坏本地/测试/迁移路径，也无法证明所有生产入口已切换 |
| M2 | **管理库事实仍存在多口径** | `management.db`、`job_runs.db`、`meta.db` 均能在不同路径承载部分管理或运行表 | 数据中心、任务中心和恢复逻辑可能读取不同事实 |

### 6.4 已确认但尚未闭环的后端缺陷

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| B1 | **业务 stale Run 回收条件失效** | `biz/tasks.py:315-318`、`biz/db.py:944-946` | `heartbeat_at` 使用 `YYYY-MM-DDTHH:MM:SSZ`，SQL `datetime()` 使用空格格式，当前 TEXT 比较不能可靠识别超时；Worker 崩溃后的 `running` 任务可能永久不被回收 |
| B2 | **业务 Blueprint 双前缀注册造成路由冲突** | `web/app.py:3041-3042` | 同一个 `biz_api` 同时注册 `/api/biz` 和 `/api`；与旧 `/api/health/details`、`/api/system/alerts` 等路径重叠，可能静默遮蔽新接口 |
| B3 | **Quality coverage 使用自证基准** | `ops/task_execution.py:95-100` | 将候选版本自身 `symbol_count` 传为 `expected_symbols`，覆盖率可能恒为 1.0；无法发现 Universe 缺失，质量门禁失去覆盖约束 |
| B4 | **Publisher 缺少同分区并发互斥** | `warehouse/publish.py:17-61` | 两个版本可同时替换同一正式文件并更新 `dataset_current`，文件、Current 指针和 previous_version 可能不一致 |
| B5 | **旧 `job_runs.db` 收敛缺少专项迁移方案** | `ops/job_runs.py`、`ops/management_db.py` | 旧库仍可能承载 TaskCenter 运行表；缺少 Legacy ID 映射、重复记录处理、只读切换和独立验收，不能证明 `management.db` 已成为唯一数据任务事实源 |
| B6 | **领域运行状态与 BusinessJobRun 状态未收敛** | `biz/task_registry.py:206-213`、`biz/repo.py:341-354` | 模拟执行在 executor 前创建 `running` 领域对象，异常时只由任务层标记 JobRun failed；领域运行可能永久停留 `running`，筛选失败还会留下已保存的 ScreenVersion/UniverseSnapshot 孤儿记录 |
| B7 | **业务队列领取不是原子 claim** | `biz/tasks.py:161-172`、`biz/tasks.py:196-203` | `run_next()` 先查询 requested 行，再由 `execute_run()` 单独校验和更新；并发 Worker 可能选中同一 Run，锁竞争被 API 转成 500，而不是可预期的 claim 失败/重试结果 |
| B8 | **新 biz 模拟事件未形成持久化事实** | `biz/simulation.py:77,287`、`biz/db.py:292-299`、`biz/repo.py` | `SimulationExecutor` 生成 `SimulationEvent` 并暂存内存，但新 biz repository 和 task handler 没有写入/查询事件的方法；模拟信号、拒单、成交和数据缺口在任务结束后丢失 |
| B9 | **DataContext 公共契约与实现字段漂移** | `docs/DOMAIN_MODEL_AND_CONTRACTS.md:182-201`、`biz/models.py:48-78`、`warehouse/datasets.py:78-87` | 文档、`DataContext` 和 `DatasetResult.context` 分别使用不同的版本、日期和 fallback 字段；下游无法稳定依赖同一数据上下文 |
| B10 | **旧 RuleContext 仍是正式旧链路依赖** | `strategy/context.py:23-52`、`core/engine.py:220-232`、`portfolio/advisor.py` | 新契约已指定 `StrategyContext`，但旧 core/portfolio/strategy 链路仍直接使用 `RuleContext`；若按文档误删会破坏旧入口 |
| B11 | **数据源统一完成标记不实** | `core/engine.py:21`、`portfolio/*`、`web/app.py` | 新 biz 链路已使用统一数据访问，但旧 Fetcher 和在线 fallback 仍被多个正式入口引用，尚未完成全量收口 |
| B12 | **Factors 废弃后的运行时残留未清理** | `config/datasets/stock_daily.yaml:120-125`、`ops/terminology.py:17-36`、相关文档/配置 | 目标已将研究因子并入 indicators，但 Factors consumer、任务类型、产物术语和历史链路仍残留，可能被误作为正式输入 |

### 6.5 产品 / 配置与扩展性（用户视角核心痛点）

| # | 问题 | 现状 | 用户期望 |
|---|------|------|------|
| U1 | **管理模块极其薄弱，基本不支持自定义配置** | `settings.html` = 4 个 YAML 文本编辑框（方案/初筛/通知规则/通知策略）+ webhook 输入 + 本金 + 数据重置 | 可视化/表单化配置，无需手写 YAML |
| U2 | **策略无「基于指标」的可视化配置，也无策略管理能力** | `schemes/*.yaml` 的 buy_rules 用 `support_level` 等**写死 type**，参数是 `support_sources:[dividend_anchor, ma_60, ...]` 写死名字；与 `schemes/indicators.yaml` 定义的指标**完全脱节**（两套平行体系） | 能「选择指标 → 组合成策略 → 保存/版本/启用停用」 |
| U3 | **通知能力薄弱** | 通知靠 3 个 YAML 手写：`rules.yaml`(自选阈值/资金流/每日汇总)、`notify_settings.yaml`(盘中频率/盘后时间/email)、`.env`(webhook)；触发条件固定为「价格突破/跌破/涨跌幅/资金流持续流入/操作建议」；**邮件 `to:` 为空未配好**；改调度需**重启容器**生效 | 定义「何时通知（时间/频率）、通过什么渠道（邮件/飞书/企微）、触发什么条件（任意指标/阈值组合）」 |
| U4 | **UI 局部历史样式和命名仍有残留** | 模板已统一继承 `base.html`，公共 CSS 和导航已建立；仍需清理局部内联样式、历史命名和页面组件差异 | 完整的布局/组件/设计系统收口 |
| U5 | **想到一点做一点，不系统** | 路由/页面/字段命名混杂（旧 `/portfolio` 重定向到 `/dashboard/warroom`、`portfolio.html` 与 `warroom.html` 并存等） | 成体系的信息架构与交互规范 |

---

## 七、关键数据指标（现状快照）

- 数据仓库：全市场日线 6435 只 × 3 年；指标分区 37 月；财务史 4551 只；PE/PB 回补 4799 只。
- 前端：模板和 JS 数量以自动检查为准；已建立 `base.html`、`base.css` 和公共导航。
- 策略：3 个内置方案（default_value / aggressive_growth / v6_si_wei）+ 指标配置 indicators.yaml。
- 通知：触发器模型 notify_rules.yaml + 渠道 feishu/wecom/email，邮件收件人可配置。

---

## 八、待讨论方向（详见正文，此处仅列骨架）

1. 策略引擎统一派发（A1）与支撑位/状态机去重（A2/A6）——「新增策略=新增 yaml + 注册 executor」真正成立。
2. 数据源统一抽象（A4）+ 个股图表读取收敛到 DuckDB（P1）——解决慢。
3. **策略配置产品化**：指标 → 策略的可视化生成 + 策略管理（U1/U2）。
4. **通知配置产品化**：时间/渠道/条件的可视化配置 + 免重启生效（U3）。
5. **前端重构**：统一 base template + 设计系统，收敛页面与导航（U4/U5）。
6. 拆 `web/app.py` + 异步化（A3）。
7. 新数据引擎切换收口：完成 `meta.db` 全量引用清理、管理库对账和生产只读观察后，将 `meta.db` 降为不可运行的归档，并在人工确认后再执行删除。

---

## 九、SRD FR-1~FR-5 落地情况（2026-08-31，持续更新）

> 依据 `docs/SRD.md` / `docs/HLD.md` 持续完成能力层收敛、两个编排器、前端基座和性能优化。下表区分代码落地与生产切换，不将模块存在或单元测试通过视为生产完成。

| 需求 | 落地 | 关键模块 | 备注 |
|------|------|----------|------|
| FR-1.1 统一规则派发 | 部分 | `strategy/rule_registry.py` `rule_builtin.py` `context.py` | 分析买入计划、Advisor 主要规则、V4.5 参数读取已接 registry；V6 仍保留决策树实现 |
| FR-1.2 支撑位/状态机去重 | 部分 | `strategy/support.py` `position_state.py` | 支撑骨架已复用；PortfolioManager/Advisor/V6 已记录共享状态，V4.5 状态仍有兼容字段 |
| FR-1.3 指标×策略打通 | 部分 | `indicators/context.py` | 买入计划/Advisor 支撑表达式已接入；旧 TechnicalIndicators 仍负责部分技术面 |
| FR-1.4 数据源抽象 + DuckDB | 部分 | `datasource/base.py` | 日线/分钟接口已抽象，部分 dashboard/engine 仍有具体源直连 |
| 指标中心 | ✅（第一版） | `indicators/store.py` `indicator_center.html` | 指标浏览/预览；自定义 base/composite CRUD；内置和代码指标保护 |
| FR-2 策略编排器 | 部分 | `core/composer.py` `scheme_store.py` `strategy_composer.html` | 结构化参数、实时预览、schema 校验、样本回测预检、默认/版本/回滚 UI 已完成；发布策略与全量引擎迁移仍进行中 |
| FR-3 通知编排器 | 部分 | `notifier/core.py` `triggers.py` `notifier/outbox.py` | action/price/indicator + AND/OR、发送后去重、持久化 outbox、重试、死信、投递台账和每日 Digest 已完成；历史兼容管线仍待清理 |
| FR-4 UI 统一 | ✅（基础完成） | `web/static/base.css` `base.html` `_nav.html` | 模板已统一继承 base.html；局部样式和组件细节仍需继续收口 |
| FR-5 性能 | 部分 | `datasource/base.py` | 个股图表 DuckDB 单查询和分钟分区已完成；未建立 P95 基准，ECharts 尚未拆包 |
| 回归 | 待重新核验 | `tests/` | 历史文档中的 116 例统计已过时；以当前实际测试结果为准 |
