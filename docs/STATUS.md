# StockInvestmentTool 整体现状与架构梳理

> 本文档为项目现状盘点：架构分层、模块实现、数据流、以及**已确认的问题清单**（架构层面 + 产品/用户视角）。
> 用途：作为后续「怎么做」改造讨论的工作底稿。定稿后可与 DESIGN.md 合并。

更新日期：2026-08-26（持续落地 SRD FR-1 ~ FR-5；真实完成度以 `docs/IMPLEMENTATION_GUIDE.md` 为准）

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
┌ 应用层    web/            Flask 蓝图(单文件 1613 行) + APScheduler 定时任务 + 16 个 Jinja 模板 + ECharts
├ 编排层    core/engine.py  统一分析管线（CLI 与 Web 共用），7 步流水线
│           core/registry.py 方案注册中心（YAML 扫描/加载/缓存/热更新）
│           core/scheme.py   YAML → SchemeConfig 数据模型
├ 业务层    portfolio/      持仓管理(manager 796行) + 买后顾问(advisor 513行) + 三页看板(dashboard 1026行)
│           strategy/       买卖决策（v4.5 与 V6.0 两代并行）
│           backtest/       回测引擎（v4.5 委托 TakeProfitOptimizer；V6.0 机械状态机）
│           notifier/ fundflow/ screener/  独立子工具
├ 指标层    indicators/      表达式引擎（基础/组合/代码指标）
├ 数据层    warehouse/       Parquet 月分区 + SQLite meta.db + DuckDB 扫描
│           datasource/      baostock(长连接+自愈) / AkShare / 腾讯行情
└ 前端      web/static/      3 个 ECharts JS + 16 个模板
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
| Web | `web/app.py` | 1613 | **单文件** Flask 蓝图，全部路由平铺 |
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
| 仓库 | `warehouse/storage.py` | 375 | Parquet 月分区 + meta.db |
| 通知 | `notifier/notify.py` + `web/scheduler.py` | 391+469 | 消息构造 + 定时调度 |
| 前端 | `web/static/*.js` | 3 文件 | echarts.min.js(1MB) + stock-chart + stock-detail |

---

## 五、设计亮点（值得保留）

1. **配置驱动策略抽象正确**：`type + params` 声明式 + `strategy_spec` 纯文档段分离，把「人能读懂的说明书」与「机器执行的参数」分开。
2. **数据层分层清晰**：`raw/daily/factors/indicators/fundamentals/online` 职责边界明确（见 DESIGN.md）。
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
| A3 | **`web/app.py` 1613 行单文件** | `web/app.py` | 路由平铺，`thread.join(300)` 伪异步占用 worker 5 分钟，难维护 |
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

### 6.3 产品 / 配置与扩展性（用户视角核心痛点）

| # | 问题 | 现状 | 用户期望 |
|---|------|------|------|
| U1 | **管理模块极其薄弱，基本不支持自定义配置** | `settings.html` = 4 个 YAML 文本编辑框（方案/初筛/通知规则/通知策略）+ webhook 输入 + 本金 + 数据重置 | 可视化/表单化配置，无需手写 YAML |
| U2 | **策略无「基于指标」的可视化配置，也无策略管理能力** | `schemes/*.yaml` 的 buy_rules 用 `support_level` 等**写死 type**，参数是 `support_sources:[dividend_anchor, ma_60, ...]` 写死名字；与 `schemes/indicators.yaml` 定义的指标**完全脱节**（两套平行体系） | 能「选择指标 → 组合成策略 → 保存/版本/启用停用」 |
| U3 | **通知能力薄弱** | 通知靠 3 个 YAML 手写：`rules.yaml`(自选阈值/资金流/每日汇总)、`notify_settings.yaml`(盘中频率/盘后时间/email)、`.env`(webhook)；触发条件固定为「价格突破/跌破/涨跌幅/资金流持续流入/操作建议」；**邮件 `to:` 为空未配好**；改调度需**重启容器**生效 | 定义「何时通知（时间/频率）、通过什么渠道（邮件/飞书/企微）、触发什么条件（任意指标/阈值组合）」 |
| U4 | **UI 不规整，像未完成品** | 16 个模板各自内联 `<style>`，`_nav.html` 用 include 内联一套 header/nav 样式对齐（注释明说「保证所有页面渲染一致」）；无统一 base template + 统一 CSS | 统一的布局/组件/设计系统 |
| U5 | **想到一点做一点，不系统** | 路由/页面/字段命名混杂（旧 `/portfolio` 重定向到 `/dashboard/warroom`、`portfolio.html` 与 `warroom.html` 并存等） | 成体系的信息架构与交互规范 |

---

## 七、关键数据指标（现状快照）

- 数据仓库：全市场日线 6435 只 × 3 年；指标分区 37 月；财务史 4551 只；PE/PB 回补 4799 只。
- 前端：17 个模板（新增 strategy_composer / notify_composer）；3 个 JS（echarts 1MB + chart 8K + detail 24K）+ base.css 设计系统。
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

---

## 九、SRD FR-1~FR-5 落地情况（2026-08-26，持续更新）

> 依据 `docs/SRD.md` / `docs/HLD.md` 完成能力层收敛 + 两个编排器 + 前端基座 + 性能优化。
> 全部改动见 git log：`feat(FR-1)/feat(FR-2)/feat(FR-3)/feat(FR-4/5)`。回归：50 个单测通过。

| 需求 | 落地 | 关键模块 | 备注 |
|------|------|----------|------|
| FR-1.1 统一规则派发 | 部分 | `strategy/rule_registry.py` `rule_builtin.py` `context.py` | 买入计划、Advisor 硬止损已接 registry；V4.5/V6 其余规则仍迁移中 |
| FR-1.2 支撑位/状态机去重 | 部分 | `strategy/support.py` `position_state.py` | 支撑骨架已复用；PortfolioManager/Advisor 已接状态机，回测仍迁移中 |
| FR-1.3 指标×策略打通 | ✅ | `indicators/context.py` | `IndicatorContext` 统一求值入口（按名/表达式/原子）；advisor 改用 |
| FR-1.4 数据源抽象 + DuckDB | ✅ | `datasource/base.py` | `DataSource` 协议 + WarehouseSource(DuckDB单查询)/OnlineSource/FallbackDataSource；monitor/dashboard 收敛 |
| 指标中心 | ✅（第一版） | `indicators/store.py` `indicator_center.html` | 指标浏览/预览；自定义 base/composite CRUD；内置和代码指标保护 |
| FR-2 策略编排器 | 部分 | `core/composer.py` `scheme_store.py` `strategy_composer.html` | 结构化 list/map-list、实时预览、schema 校验、样本回测预检已完成；发布 UI 仍补齐中 |
| FR-3 通知编排器 | 部分 | `notifier/core.py` `triggers.py` `notify_composer.html` | action/price/indicator + AND/OR 已执行；统一 outbox/批次和重试仍补齐中 |
| FR-4 UI 统一 | ✅ | `web/static/base.css` `base.html` `_nav.html` | 设计令牌+组件类+移动端；导航收敛单组件；新页面去内联 |
| FR-5 性能 | 部分 | `datasource/base.py` | 个股图表 DuckDB 单查询（P1）已完成；echarts 1MB 仅图表页加载 |
| 回归 | ✅ | `tests/`（86 例）| 新增指标、策略、通知、事务、安全和回测参数回归；完整测试 86 例通过 |
