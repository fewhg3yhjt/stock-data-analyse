# Data Source Migration Plan

> 状态更新：2026-09-08。本文描述的是当前收口状态和剩余工作，不代表最终 Legacy Cutover 已完成。

## Target

All production business reads and writes must use the new data module:

```text
Raw Batch -> Candidate -> Quality -> Publish -> DatasetAccess
management.db -> dataset_current/dataset_versions/task state
```

The legacy `meta.db` and `warehouse.db` files are not valid production data
sources. They may remain as explicitly isolated test fixtures or migration
inputs, but production code must not silently fall back to them.

## Code And Configuration Verified

- `Warehouse` 默认使用 `output/data/management.db`；生产默认路径不再回退到旧元数据库。
- `DatasetAccess` 是正式数据读取入口，读取 `dataset_current` 并校验 Published 状态、文件存在性、质量和 checksum。
- 行业成员、行业日线、行业特征和行业轮动已接入 Published 数据链路。
- 已在生产 `management.db` 中发现 stock_daily、industry、指标和行业相关任务定义，以及部分 enabled 配置；这不是完整生产闭环证据。
- 旧库仅允许作为显式迁移、备份、诊断或测试输入，不能作为生产业务读取源。

代码、配置和默认路径核对还确认：

- `web/app.py` 的股票分类接口仍可能在 fundamentals 缺失时在线采集并写仓库。
- `web/scheduler.py` 仍在 Web 进程内触发部分数据生产任务。
- `allow_legacy` 仍存在于生产可调用模块，尚未完成运行时入口分类收口。
- `instruments` 当前定位为 `management.db` 的基础标的目录服务，但最终契约边界仍需定稿。
- 生产任务定义、实际运行、版本、质量和 `dataset_current` 尚未形成全量验收证据。

## Production Evidence Still Missing

以下内容不能仅凭代码、YAML 或单次查询推断为已完成，必须生成可复核的只读审计记录：

- 检查时间、管理库路径和运行环境标识。
- 全部实际 `task_key`、`enabled`、`active_config_version`。
- 每个任务最近一次 Run、状态、周期、错误和输出。
- 每个数据集的 `dataset_current`、Version、`publish_status`、Quality、文件路径和 checksum。
- 生产运行时旧库读取/写入为零的证据。
- 移除旧库输入后的隔离环境冷启动结果。
- 旧任务台账与 `management.db` 的数量、ID、状态和重复记录对账。
- Web 原始入口到最终结果页面的完整链路验证。

建议阶段 0 生成并登记以下报告路径；报告不存在时不得引用该路径作为已完成证据：

```text
docs/audits/production_management_db_task_audit_YYYYMMDD.md
docs/audits/production_dataset_current_reconciliation_YYYYMMDD.md
docs/audits/production_legacy_runtime_reference_audit_YYYYMMDD.md
```

## Remaining Production Work

- `web/scheduler.py` 仍在 Web 进程内触发部分数据生产任务；需要按运行架构方案迁移到独立 Data Worker。
- `web/app.py` 的股票分类入口仍需完成 fundamentals 的 Published-only 收口，禁止页面请求触发网络采集和写仓库。
- `allow_legacy`、旧库参数和兼容分支仍需按“生产运行时/测试/迁移/归档”分类审计；不能仅凭字符串扫描删除测试和一次性迁移能力。
- `instruments` 当前是 `management.db` 中的基础标的目录服务，不等同于行情 Published Dataset；后续需固定其服务边界，禁止旧库兜底。
- `strategy_lab.py`、portfolio、core 和 datasource 仍需按实际生产入口逐一验证是否只读 Published 数据或明确的业务目录服务。
- `ops/management_db.py`、迁移脚本和备份脚本保留显式历史输入能力，但不得从 Web、Scheduler 或 Worker 运行时调用旧库输入。

## Execution Order

1. Keep existing Published files and versions intact; do not overwrite or delete market data.
2. Confirm all YAML task definitions are active in production `management.db`.
3. Keep industry K-line, feature and rotation reads on Published datasets only.
4. Move data production out of the Web process before closing remaining runtime paths.
5. Replace remaining production legacy reads one subsystem at a time with `DatasetAccess` or an explicit management database service.
6. Remove legacy fallback APIs from production-callable modules after tests are migrated to Published fixtures.
7. Complete management/data-version reconciliation and isolated cold-start verification.
8. Only then demote old files to read-only archive; physical deletion requires separate confirmation.

## Verification

- Classify every old-library reference as production runtime, test fixture, migration input, backup, diagnostic or documentation.
- Audit runtime references after each migration step; a reference in a migration tool is not a production dependency.
- Confirm `dataset_current` and `dataset_versions` point to the expected
  Published files.
- Confirm each task has an active definition and a recorded run.
- Confirm web responses include explicit dataset category and `as_of` values.

## Current Closure Checklist

以下事项当前均未完成：

- 财务分类接口 Published-only 收口。
- Scheduler 与 Web 数据生产职责分离。
- Data Worker 生产接管。
- 生产运行时旧库读写为零的证明。
- 全量任务 active/enabled/run/output/quality/current 对账。
- `dataset_current`、版本记录、Published 文件和 checksum 对账。
- 移除旧库输入后的隔离环境冷启动。
- 旧任务台账与 `management.db` 对账及只读观察。

## Stage 0 Baseline

阶段 0 只读基线已完成，报告已登记：

- [生产管理库任务审计](audits/production_management_db_task_audit_20260908.md)
- [生产 Dataset Current 对账](audits/production_dataset_current_reconciliation_20260908.md)
- [生产旧库运行时引用审计](audits/production_legacy_runtime_reference_audit_20260908.md)

阶段 0 只证明当前事实和缺口，不代表 Data Worker 已建立、Scheduler 已停止执行数据生产，或旧库已经完成零读写验收。

阶段 1 已新增 Data Worker 最小运行骨架，但服务仍处于显式 `data-worker` Compose profile，未接管生产数据任务；详见 [运行架构拆分方案](RUNTIME_ARCHITECTURE_MIGRATION_PLAN.md) 的阶段 1 实施记录。

阶段 2 已将 Web Scheduler 的数据生产调用改为 Request 入队；Data Worker 尚未在生产中启用，故当前仍是“新调度契约已建立、生产执行者未切换”的过渡状态。

2026-09-08 已完成生产进程切换：`stock-invest-data-worker` 已启动并保持 healthy，`stock-web` 已重启加载只入队 Scheduler，`stock-invest-business-worker` 保持运行。切换后队列为空，尚未执行切换后的真实数据任务，因此“执行角色已切换”与“新进程已完成真实采集闭环”需要分开验收。

受控验证期间 Scheduler 自动创建了 6915 标的的全市场 Request。经确认后已停止 Data Worker 并将 Request、JobRun、SourceBatch 收口为失败；已产生 Raw 文件保留，未生成新的 Published Version 或更新 Current。Data Worker 当前保持停止，重新验证前必须先增加受控范围保护，不能直接重启后等待 Scheduler 自动触发。

停机隔离期间已完成正式数据接口收口：默认业务数据源只读取 Published Dataset；Web 分类、Core 分析、Portfolio 基本面快照和 Biz 指标触发器不再直接在线采集、写回仓库或使用隐式 legacy fallback。相关数据源回归 31 项通过；通知规则数量和 `watch_pool` fixture 的既有测试问题仍未处理。Web/Business Worker 恢复后，Data Worker 仍需在受控范围保护下单独启动验证。

生产验证结果：容器内按 symbol 精确读取 Published `fundamentals/sh600000` 约 `0.061s`，`/api/classify?code=600000` 原始入口返回 HTTP 200；Web 和 Business Worker 已恢复 healthy，Data Worker 保持停止。此次没有执行真实数据 Request，也没有新增 Dataset Version 或更新 Current。

正式 Web、Core、Portfolio、Biz 和 Comparison 入口的在线数据 fallback 扫描已清零；显式 `FallbackDataSource`、`OnlineSource` 和迁移能力仅保留给测试/隔离研究。分红 Published 契约尚未建立，正式股息锚现在明确返回不可用。
