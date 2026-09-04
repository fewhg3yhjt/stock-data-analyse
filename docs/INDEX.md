# StockInvestmentTool 文档索引

> 项目内部文档门户。涉及生产环境、真实路径和内部实现，仅限项目内部使用。

文档阅读顺序和规范等级：`DOMAIN_MODEL_AND_CONTRACTS.md` 定义公共契约；`DATA_PIPELINE_V1_DESIGN.md` 定义数据平面；各专项设计定义模块契约；`HLD.md` 只做架构总览；`STATUS.md` 只描述当前实现；`DATA_PIPELINE_STATEFLOW_REMEDIATION_PLAN.md` 只描述待办和验收，不代表完成。

## 核心设计

- [投资分析工具产品总纲](PRODUCT_BLUEPRINT.md)：产品目标、用户主流程、业务模块边界、统一实体和第一版范围。
- [投资产品领域模型与统一契约](DOMAIN_MODEL_AND_CONTRACTS.md)：新旧体系真源、旧骨架迁移、统一实体、状态和平台任务接入边界。
- [数据平台与可信数据链路设计](DATA_PIPELINE_V1_DESIGN.md)：数据集生命周期、真实输入契约、指标架构、Universe、质量、版本、发布和业务能力可用性矩阵。
- [旧体系下线与新体系切换计划](LEGACY_CUTOVER_PLAN.md)：禁止兼容并行、一次性迁移、切换步骤和下线验收。
- [新系统持久层设计](NEW_SYSTEM_STORAGE_DESIGN.md)：新业务库、实体落表、键和索引、账户初始化、迁移和对账。
- [架构设计总纲](DESIGN.md)：项目分层、模块职责和总体数据流。
- [概要设计说明书](HLD.md)：核心能力抽象与目标架构。
- [废弃：旧后台领域设计 V1.0](BACKEND_RESEARCH_SIMULATION_STRATEGY_V1.md)：仅供历史数据评估和一次性迁移，禁止用于新功能和运行时实现。
- [任务与数据术语](TASK_DATA_GLOSSARY.md)：任务阶段、任务类型、数据产物、状态和数据中心分类的统一用户用词。
- [任务与数据中心前端设计](TASK_DATA_CENTER_DESIGN.md)：任务中心、数据中心的用户信息架构、操作和 API 契约。
- [核心页面 UI 设计与前端契约](CORE_PAGES_UI_DESIGN_V1.md)：全部页面的目标信息架构、设计 Token、公共组件、数据口径和验收规范。
- [数据链路与产品状态流转收口实施任务书](DATA_PIPELINE_STATEFLOW_REMEDIATION_PLAN.md)：针对当前双轨数据链路、任务状态、质量门禁和下游消费断点的分阶段实施计划、测试矩阵与验收标准。
- [指标归一后续改造清单](INDICATOR_NORMALIZATION_BACKLOG.md)：指标口径归一改造中识别出的遗留边界项（数据源契约、数据接管、回测基线重录等）。

## 需求与现状

- [需求规格](SRD.md)：产品需求和功能边界。
- [整体现状与架构梳理](STATUS.md)：当前实现、已知问题和技术债。
- [目标架构设计](TARGET_ARCHITECTURE.md)：新项目独立重建方案、统一业务闭环、数据平面、策略运行时、任务模型和验收标准。
- [Legacy 项目参考指南](LEGACY_REFERENCE_GUIDE.md)：新项目协作时读取当前项目的范围、优先级、Token 使用和历史导入规则。
- [后续开发执行指南](IMPLEMENTATION_GUIDE.md)：生产约束、开发顺序和后续 AI 接手规范。
- [交接说明](HANDOVER.md)：项目关键上下文和接手提示。

## 专项设计与实施

- [公开市场数据 API](PUBLIC_MARKET_DATA_API.md)：面向在线大模型和外部工具的无认证只读数据接口、参数、字段、分页与调用示例。
- [策略核心与回测模拟子模块设计](STRATEGY_CORE_AND_SIMULATION_DESIGN.md)：指标条件、策略编排、统一决策协议和回测/模拟执行模型。
- [选股与行情分析子模块设计](SCREENING_AND_MARKET_ANALYSIS_DESIGN.md)：筛选方案、筛选运行、候选追溯、走势图查询及观察池/模拟衔接。
- [个股研究与分析子模块设计](RESEARCH_AND_ANALYSIS_DESIGN.md)：ResearchRun、研究证据、结构化研究结果与策略决策衔接。
- [观察池与关注列表子模块设计](OBSERVATION_AND_WATCHLIST_DESIGN.md)：区分筛选候选、用户关注和观察周期，定义模拟及真实建仓衔接。
- [账户、持仓与交易子模块设计](ACCOUNT_PORTFOLIO_AND_TRADING_DESIGN.md)：账户、持仓周期、实际成交、现金流水、成本核算和状态流转。
- [收益分析与复盘子模块设计](PERFORMANCE_AND_REVIEW_DESIGN.md)：实际收益、策略模拟、基准对比、执行偏差和交易周期复盘。
- [建议与消息通知子模块设计](ADVICE_AND_NOTIFICATION_DESIGN.md)：建议生命周期、通知事件、邮件投递、去重、重试和系统告警。
- [平台支撑与运行治理子模块设计](PLATFORM_RUNTIME_AND_OPERATIONS_DESIGN.md)：任务运行、调度恢复、健康检查、权限、备份和 API 平台规范。
- [Phase 4 设计](PHASE4_DESIGN.md)
- [Phase 4 实施记录](PHASE4_IMPLEMENTATION.md)
- [市场发现待办](MARKET_DISCOVERY_TODO.md)
- [行业数据与轮动观测规范](DATA_PIPELINE_V1_DESIGN.md#29-行业数据与轮动观测规范)

## 前端

- [前端工程整改清单](FRONTEND_ENGINEERING_BACKLOG.md)：颜色语义、XSS/CSP、Toast/公共函数、CDN、静态资源版本和运行时问题的整改状态。
- [前端实现状态](UI_IMPLEMENTATION_STATUS.md)：当前页面和公共资源的实际落地范围、验证基线和已知限制。

## 运维

- [备份运行手册](BACKUP_RUNBOOK.md)
