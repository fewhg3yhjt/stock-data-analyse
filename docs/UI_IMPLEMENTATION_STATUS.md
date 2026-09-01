# 前端实现状态

> 本文只记录当前代码的实际实现状态，不定义新的页面目标。
>
> 页面目标设计、Token 和组件契约见 `CORE_PAGES_UI_DESIGN_V1.md`；工程问题和整改任务见 `FRONTEND_ENGINEERING_BACKLOG.md`。

## 总体状态

工程进度只使用 `todo`、`in_progress`、`blocked`、`done`、`wont_fix`。页面中的 `healthy`、`running`、`failed` 等仍属于业务运行状态，不表示前端工程进度。

| 项目 | 当前状态 | 说明 |
|---|---|---|
| 页面信息架构 | `done` | V1 页面分类和目标已记录在设计文档中 |
| 公共页面壳层 | `in_progress` | `base.html` 已加载公共资源；部分旧模板仍通过 `.app` 和 CSS 适配，尚未全部改为统一结构 |
| 工作台 | `in_progress` | `wb-*` 独立壳层、摘要、待办、持仓、观察池、数据和通知区域已实现；仍消费旧聚合接口，市场摘要为空 |
| 数据中心 | `in_progress` | `dm-*` 壳层、资产状态、生产链路、操作反馈和详情抽屉已实现；部分运行编号和安全预览受接口限制 |
| 任务中心 | `in_progress` | 分组、运行状态、配置、日志、产物和轮询已实现；部分执行场景不返回本次 `run_id` |
| 个股研究详情 | `in_progress` | ResearchRun 创建/查询、业务运行轮询、四维评估、证据和数据上下文已接入；报告和后续动作仍按 API 状态收口 |
| 状态词表 | `in_progress` | `ui-status.js` 已全局加载并提供 canonical class；部分旧页面仍有局部状态文案和样式待收口 |
| 反馈机制 | `done` | `showUiMessage()`、`showUiConfirm()` 和 `showUiInput()` 已由公共资源提供；页面已不再调用原生 `alert/confirm/prompt` |
| 设计 Token | `done` | `base.css` 与页面/脚本已改用 `--color-*` 语义 Token；旧变量及模板内联 `:root` 已删除，全库 `var()` 引用均有定义 |
| 状态 CSS 类 | `done` | `ui-status.js` 的 `cls` 已返回完整 `status-*` canonical class；核心页面已统一使用 `.status-tag.status-<key>` |
| 公共组件类名 | `in_progress` | 基础 `.ui-*` 组件和主要类名迁移已完成；旧组件类、页面内联样式及独立 `wb-*`/`dm-*` 体系仍未全部收口 |
| 页面级 Adapter | `in_progress` | 核心系统页面有独立脚本，许多业务页仍是模板内联脚本 |
| 安全治理 | `in_progress` | 内联事件注入已清零，Markdown 已通过 `ui-sanitize.js` 净化，CSP 和基础安全头已启用；仍允许内联脚本和 CDN |
| 资源治理 | `in_progress` | ECharts 已统一使用本地资源，静态资源版本已改为按文件修改时间生成；Font Awesome、Marked、内联脚本和构建策略仍待治理 |
| 人工浏览器验收 | `todo` | 已做路由/接口冒烟，尚未完成逐页视觉、移动端和交互验收 |

## 页面实现矩阵

页面矩阵中的工程进度只使用上述五种状态；业务运行状态和页面数据状态按设计文档中的独立维度记录。

| 页面 | 路由 | 当前壳层 | 当前实现 | 主要限制 | 状态 |
|---|---|---|---|---|---|
| 工作台 | `/`、`/workbench` | `wb-*` | 独立 JS，摘要和待办已接入 | 仍消费旧聚合接口，市场摘要为空 | `in_progress` |
| 数据中心 | `/data-center` | `dm-*` | 独立 Adapter，资产、健康、链路已接入 | 任务运行编号和安全预览受接口限制 | `in_progress` |
| 数据资产 | `/data-center/assets` | `dm-*` | 目录、动态分类、详情和管理操作 | 表单校验和脚本模块化仍需收口 | `in_progress` |
| 任务中心 | `/data-center/tasks` | `dm-*` | 分组、筛选、配置、日志、产物和轮询 | 执行接口部分场景不返回本次 `run_id` | `in_progress` |
| 市场 | `/market` | `.app` 公共适配 | 指数、板块、持仓图表保留 | 模板内联脚本和 CDN 依赖 | `in_progress` |
| 市场发现 | `/market-discovery` | `.app` 公共适配 | 筛选、分页、K 线和观察入口保留 | 候选上下文和内联脚本仍需收口 | `in_progress` |
| 观察池 | `/watch-pool` | `.app` 公共适配 | 来源、状态、模拟和建仓入口保留 | 入口与观察看板/自选仍有重叠 | `in_progress` |
| 观察池行情 | `/dashboard/observe` | `.app` 公共适配 | 行情、支撑、指令和详情保留 | 超宽表格仍需移动端收口 | `in_progress` |
| 自选 | `/watchlist` | `.app` 公共适配 | 关注关系、加入时间、备注和模拟 | 页面结构和脚本模块化仍需收口 | `in_progress` |
| 持仓 | `/dashboard/warroom` | `.app` 公共适配 | 账户摘要、风险、建议和事务入口 | 持仓操作实现未完全共用 | `in_progress` |
| 持仓管理 | `/portfolio` | `.app` 公共适配 | 持仓事实、交易、导入导出 | 与持仓主页面仍有入口重叠 | `in_progress` |
| 持仓详情 | `/portfolio/{id}` | `.app` 公共适配 | 成本、流水、建议和操作 | 页面结构和表单校验仍需收口 | `in_progress` |
| 模拟 | `/simulation` | `.app` 公共适配 | 模拟清单、重算、收益和建仓入口 | 模拟与观察池页面边界仍需继续收口 | `in_progress` |
| 复盘 | `/dashboard/review` | `.app` 公共适配 | 统计、FIFO 流水、手动记录和导出 | 仍以服务端渲染和内联脚本为主 | `in_progress` |
| 晨报 | `/morning-report` | `.app` 公共适配 | Markdown 摘要和生成入口已接入净化 | 页面脚本和资源治理仍需收口 | `in_progress` |
| 操作日志 | `/log` | `.app` 公共适配 | 业务操作和成交记录展示 | 记录类型和统一反馈需继续整理 | `in_progress` |
| 快速记录 | `/quicklog` | `.app` 公共适配 | 快速录入入口 | 页面脚本和交互仍较旧 | `in_progress` |
| 研究中心 | `/research` | `.app` 公共适配 | 研究工具入口和流程说明 | 入口页，不承载正式研究结果 | `in_progress` |
| 个股研究 | `/research/detail` | `.app` 公共适配 | ResearchRun 结构化结果和上下文 | 报告、快照和模拟后续动作按 API 状态接入 | `in_progress` |
| 个股分析 | `/analyze` | `.app` 公共适配 | 行情、方案、指标、报告和回测 | 进度展示和旧脚本需整改 | `in_progress` |
| 指标中心 | `/indicator-center` | `.app` 公共适配 | 指标目录、编辑和预览 | 表单校验和内联脚本仍需收口 | `in_progress` |
| 策略编排 | `/strategy-composer` | `.app` 公共适配 | 规则、方案、校验、发布和回滚 | 确认面板和脚本拆分未完成 | `in_progress` |
| 策略实验 | `/strategy` | `.app` 公共适配 | 扫描、回测、曲线和明细 | CDN、内联脚本和运行状态需收口 | `in_progress` |
| 操作点位 | `/operation-points` | `.app` 公共适配 | 点位、支撑、止损、RR 和回测 | 表单组件契约尚未完全落地 | `in_progress` |
| 方案对比 | `/compare` | `.app` 公共适配 | 方案选择、收益对比和报告 | 内联样式和资源治理需整改 | `in_progress` |
| 通知中心 | `/notify-center` | `.app` 公共适配 | 规则、通道、投递台账、重试 | 业务通知/系统告警尚未完全 Tab 化 | `in_progress` |
| 运行诊断 | `/diagnostics` | `.app` 公共适配 | 只读健康检查和状态表 | 状态 CSS 和独立脚本仍可继续收口 | `in_progress` |
| 系统 | `/system` | `.app` 公共适配 | 系统工具入口和职责说明 | 入口页，数据仍由子页面负责 | `in_progress` |
| 系统设置 | `/settings` | `.app` 公共适配 | 方案、规则、通知、账户和重置 | 内联样式和表单契约需整改 | `in_progress` |
| 登录 | `/login` | 独立登录布局 | 登录表单和错误提示 | 不使用业务侧栏 | `in_progress` |

## 公共资源现状

| 资源 | 当前作用 | 当前问题 |
|---|---|---|
| `base.css` | 公共 Token 和基础组件 | 语义 Token 和 `.ui-*` 基础组件已存在；旧类和页面内联样式仍并存 |
| `ui-shell.css` | 旧 `.app` 页面公共壳层适配 | 仍通过选择器覆盖旧模板结构 |
| `ui-status.js` | 状态词表和 canonical class 映射 | `cls` 已返回完整 `status-*` 类名；部分旧页面仍需统一调用和展示 |
| `ui-feedback.js` | 全局页面消息和旧 alert 过渡 | 不应继续劫持 `window.alert` 作为最终方案 |
| `ui-utils.js` | 公共转义和基础格式化 | 已接入核心 Adapter；页面内联脚本仍有重复辅助函数 |
| `workbench.css/js` | 工作台独立页面 | 与数据模块壳层仍有重复样式 |
| `data-module.css` | 数据中心和任务中心 | 仍有页面级内联样式 |
| `data-center-adapter.js` | 数据总览 Adapter | 资产目录页仍有独立内联渲染逻辑 |
| `research-detail.js/css` | 研究详情页 | 仍需完善上下文字段和后续动作展示 |
| `stock-chart.js` | 通用 ECharts 组件 | 统一提供图表挂载和释放入口；K 线、成交量和均线颜色已按契约统一 |
| `market.css/js` | 市场页独立资源 | 市场页样式和图表业务脚本已从模板移出；仍依赖现有 CDN 图标资源 |

## 最近验证基线

| 验证项 | 最近结果 | 备注 |
|---|---|---|
| 全量 Python 测试 | 以当前测试收集结果和 CI 为准 | 历史通过数仅作记录，代码变更后必须重新执行 |
| JavaScript 语法 | 通过 | 已对 `web/static/*.js` 逐文件检查 |
| 页面路由冒烟 | 主要页面 HTTP 200 | 不等于浏览器交互或视觉验收 |
| 生产容器 | `healthy` | 代码挂载模式，纯代码改动通过 restart 生效 |

## 更新规则

- 页面或整改事项完成后先更新本文，再更新 `FRONTEND_ENGINEERING_BACKLOG.md` 中对应条目状态。
- 不在 `CORE_PAGES_UI_DESIGN_V1.md` 写临时测试数字、提交哈希或当前实现细节。
- 本文的工程状态只表示对应实现事项的进度，不表示业务运行状态或最终浏览器验收结果。
- 发现新问题时记录真实表现、影响、是否需要后台配合和验收方法。
