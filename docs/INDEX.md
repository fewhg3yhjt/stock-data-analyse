# StockInvestmentTool 文档索引

> 项目内部文档门户。涉及生产环境、真实路径和内部实现，仅限项目内部使用。

## 核心设计

- [架构设计总纲](DESIGN.md)：项目分层、模块职责和总体数据流。
- [概要设计说明书](HLD.md)：核心能力抽象与目标架构。
- [可信数据链路设计与实施规范 V1.1](DATA_PIPELINE_V1_DESIGN.md)：`stock_daily` 从元数据、采集、Raw Batch、构建、质量、发布到下游消费的专项设计、实施阶段和验收基准。
- [任务与数据术语](TASK_DATA_GLOSSARY.md)：任务阶段、任务类型、数据产物、状态和数据中心分类的统一用户用词。

## 需求与现状

- [需求规格](SRD.md)：产品需求和功能边界。
- [整体现状与架构梳理](STATUS.md)：当前实现、已知问题和技术债。
- [后续开发执行指南](IMPLEMENTATION_GUIDE.md)：生产约束、开发顺序和后续 AI 接手规范。
- [交接说明](HANDOVER.md)：项目关键上下文和接手提示。

## 专项设计与实施

- [Phase 4 设计](PHASE4_DESIGN.md)
- [Phase 4 实施记录](PHASE4_IMPLEMENTATION.md)
- [市场发现待办](MARKET_DISCOVERY_TODO.md)

## 运维

- [备份运行手册](BACKUP_RUNBOOK.md)
