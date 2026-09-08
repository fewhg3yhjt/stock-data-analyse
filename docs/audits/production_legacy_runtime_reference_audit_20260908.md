# Production Legacy Runtime Reference Audit

> 审计日期：2026-09-08
> 审计性质：阶段 0 只读代码和进程基线
> 审计范围：Web、Scheduler、Business Worker、Data Worker、旧库和 legacy 访问能力
> 结论：这是引用分类基线，不代表生产旧库零读写验收已完成。

## Runtime Topology

| Role | Current evidence | Stage 0 classification |
|---|---|---|
| `stock-web` | Flask/Waitress + APScheduler，状态 healthy | Web 与部分数据生产仍混合 |
| `stock-invest-business-worker` | `python -m StockInvestmentTool.biz.worker`，状态 healthy | 业务任务 Worker |
| Data Worker | Compose 中没有独立服务 | 未建立 |
| Scheduler | `web/scheduler.py` 由 Web 进程加载 | 尚未拆分 |

## Direct Data Production Calls

当前代码扫描确认：

- `web/scheduler.py` 直接引用 `IndustryCollector`、`stage_and_publish_industry_batch` 和 `execute_task`。
- `ops/task_execution.py` 直接实现 `MarketCollector`、`IndustryCollector`、`IndicatorsBuilder`、`IndustryFeaturesBuilder`、`IndustryRotationBuilder` 和 `Publisher` 的任务执行分派。
- `web/app.py` 直接引用 `Publisher`、`IndicatorsBuilder` 和 `execute_task`，并暴露数据任务相关入口。
- `web/app.py` 的 `api_classify` 在 fundamentals 缺失时调用 `get_fundamental_history()` 并写回仓库。
- `portfolio` 目录本轮未发现上述数据生产类的直接引用，但仍需按完整业务入口审计数据源 fallback。

## Legacy Reference Classification

| Reference type | Current classification | Required handling |
|---|---|---|
| `DatasetAccess.allow_legacy` | 生产可调用模块中的显式能力 | 逐入口确认；生产正式入口关闭，测试/迁移显式保留 |
| `meta.db` / `job_runs.db` in migration tools | 一次性迁移或历史输入 | 保留显式参数，不得由运行时调用 |
| old database names in tests | 测试 fixture | 使用临时隔离库，不触碰生产数据 |
| old database names in backups/diagnostics | 备份或诊断输入 | 只读，明确历史/归档语义 |
| `StockDataFetcher` in old Web/core/portfolio paths | 尚未完成的正式入口收口 | 逐入口改为 Published/统一数据访问或明确在线研究路径 |

## Findings

1. 生产默认 `MANAGEMENT_DB_PATH` 已指向容器内 `management.db`，但这只能证明默认路径，不证明所有运行时访问已收口。
2. 当前没有独立 Data Worker，且 Web Scheduler 仍可直接触发数据生产。
3. Business Worker 与 Data Worker 的职责隔离尚未形成进程级证据。
4. 不能通过全局搜索旧库名称判断迁移完成，也不能仅凭 `Warehouse()` 默认路径判断生产旧库零读写。
5. 阶段 0 未执行动态系统调用追踪，因此“旧库读写为零”仍是待验证项。

## Required Post-Implementation Checks

- Data Worker 启动并独立运行，Web/Scheduler 不再导入或调用 Collector/Builder/Quality/Publisher。
- 终止 Data Worker 后 Web 仍健康，Run 最终进入 failed/timeout，重启后仅恢复未完成批次。
- 终止 Web 后 Data Worker 不受影响。
- 终止 Business Worker 后 Data Worker 不受影响，且业务 Worker 不写 `stock_daily`。
- 隔离移除旧库后 Web、Scheduler、Data Worker、Business Worker 和 DatasetAccess 冷启动成功。
