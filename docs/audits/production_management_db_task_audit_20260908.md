# Production Management DB Task Audit

> 审计日期：2026-09-08
> 审计性质：阶段 0 只读基线
> 审计范围：生产 `output/data/management.db` 的任务定义、配置状态和近期运行事实
> 结论：不代表数据平面已切换到独立 Data Worker，也不授权修改或删除生产数据。

## 环境

- 工作区：`/opt/stock_data_analyse`
- 管理库：`/opt/stock_data_analyse/output/data/management.db`
- Compose Web 容器：`stock-invest`，状态 `running/healthy`
- 业务 Worker：`stock-invest-business-worker`，状态 `running/healthy`
- 独立 Data Worker：未发现
- 检查方式：宿主机只读 SQLite 查询；未执行采集、构建、质量或发布任务

## Task Definitions

管理库共发现 25 个任务定义，均有 `active_config_version`，其中 12 个 enabled，13 个 disabled。

### Enabled

```text
factors_build
indicators_build
industry_capture
industry_daily_capture
industry_features_build
industry_rotation_build
money_flow_capture
stock_daily_build
stock_daily_capture
stock_daily_publish
stock_daily_quality
valuation_capture
```

### Disabled

```text
financial_reports_build
financial_reports_capture
financial_reports_publish
financial_reports_quality
fundamentals_capture
industry_membership_capture
valuation_daily_build
valuation_daily_publish
valuation_daily_quality
valuation_snapshot_build
valuation_snapshot_capture
valuation_snapshot_publish
valuation_snapshot_quality
```

## Recent Runs

最近运行事实表明数据任务仍由现有运行环境触发，而不是由独立 Data Worker 接管：

- `industry_daily_capture` 最近运行成功，周期为 `2026-09-08`。
- `industry_features_build` 最近记录为成功，最新运行日期为 `2026-09-04`。
- `stock_daily_capture` 最近多次失败，周期主要为 `2026-09-07`，错误为“任务进程已结束，运行记录自动回收（取消孤儿任务）”。
- `stock_daily_capture` 最近一次记录：`2026-09-08T20:00:01` 开始，`2026-09-08T20:01:32` 结束，状态 `failed`。
- `industry_rotation_build` 没有出现在最近 15 条目标任务运行记录中；其 Published 版本存在，但不能由此推断当前任务闭环持续正常。

## Findings

1. 任务定义存在且大部分配置版本 active，但这不等于每个任务已经形成 `enabled + successful run + output version + quality + current` 的完整闭环。
2. `stock_daily_capture` 当前有连续失败/孤儿回收事实，不能标记为生产采集健康。
3. Compose 当前只有 Web 和 Business Worker，没有 Data Worker。
4. 阶段 0 结论：可以进入 Data Worker 设计和实现阶段，但必须保留当前失败事实，并在后续验证中解决任务分批、恢复和职责隔离问题。

## Next Evidence

- 逐任务补齐最近 Run、输出版本、质量结果和 Current 指针的关联审计。
- Data Worker 建立后，证明 Scheduler 只创建 Request，不直接执行数据生产。
- 对 `stock_daily_capture` 做小范围隔离故障演练，验证失败收口和 checkpoint/resume。
