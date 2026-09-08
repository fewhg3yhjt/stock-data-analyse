# 项目修复进度报告

> 报告日期：2026-09-01
> 报告性质：生产运行修复与旧数据迁移审计记录
> 本报告不代表旧库已经完成迁移，也不授权删除任何历史数据库或数据文件。

## 1. 本次处理范围

本次只处理影响当前业务运行的数据管理元数据问题，并完成只读迁移审计：

1. 备份生产 `management.db`。
2. 修复 `stock_daily/2026-08` 的错误 Current 指针。
3. 校验所有 `dataset_current` 对应文件的存在性和 checksum。
4. 验证业务数据读取和 `signal_scan`。
5. 审计旧库与新库的记录规模、迁移映射和可删除性。
6. 增加测试运行时数据库隔离，防止继承生产环境变量。

未执行：

- 删除旧数据库；
- 删除历史 Factors 文件；
- 覆盖或重建 Parquet 数据；
- 删除旧代码或兼容入口；
- 修改其他 AI 正在处理的前端文件。

## 2. 生产问题与修复

### 2.1 问题

生产 `management.db` 中当前版本：

```text
dataset: stock_daily
partition: 2026-08
```

曾错误指向测试临时路径：

```text
/tmp/pytest-of-root/pytest-10/test_access_reads_current_only0/warehouse/daily/2026-08.parquet
```

实际生产文件位于：

```text
/app/StockInvestmentTool/output/data/warehouse/daily/2026-08.parquet
```

该问题会阻断 2026-08 分区的 `DatasetAccess` 读取，进一步影响筛选、研究和模拟。

### 2.2 修复

修复前已生成 SQLite 备份：

```text
output/backups/management-before-path-repair-20260901T112031Z.db
```

备份执行后 `PRAGMA quick_check=ok`。

已将 `dataset_current(stock_daily, 2026-08)` 切换到既有生产版本：

```text
stock_daily_202608_ef76748a868c
```

生产文件 checksum：

```text
96c0bffd4a99c392eb323df0c80d837c17d90bfcd17988630427420c37a597eb
```

Current 指针、Published Path 和 checksum 已一致。

## 3. 生产验证结果

### 3.1 Current 数据集完整性

对生产 `management.db` 中全部 `dataset_current` 记录执行只读校验：

```text
Current 记录数：4700
缺失文件：0
checksum 不匹配：0
```

覆盖数据集包括：

```text
industry          1
money_flow_daily   1
stock_daily       37
valuation_daily   37
indicators        37
factors           13
fundamentals   4574
```

说明：历史 `dataset_versions` 中仍有许多非 Current 版本复用当前文件路径但 checksum 不同，这些属于历史版本记录问题，不等同于当前正式文件损坏。本次没有批量修改历史版本记录。

### 3.2 业务读取

已在生产容器内完成有限范围 `signal_scan` 冒烟：

```text
日期范围：2026-08-01 至 2026-08-31
证券范围：4 只
读取行数：80
命中证券：4
候选去重：通过
实际数据截至：2026-08-28
耗时：约 0.775 秒
```

### 3.3 服务健康

```text
stock-invest：healthy
stock-invest-business-worker：healthy
management.db：healthy
business_queue：healthy
business_worker：healthy
整体 readiness：ok
```

## 4. 测试隔离修复

测试公共 fixture 已默认清除：

```text
MANAGEMENT_DB_PATH
BUSINESS_DB_PATH
```

单个测试如需使用指定路径，必须显式通过 `monkeypatch` 设置。验证结果：即使测试进程继承生产 `MANAGEMENT_DB_PATH`，数据访问、发布锁、质量传播和业务健康相关测试仍使用临时库。

```text
隔离环境回归：46 passed
继承生产管理库环境变量的隔离回归：32 passed
```

## 5. 旧库迁移审计

### 5.1 数据库规模

| 数据库 | 表数 | 非空事实摘要 |
|---|---:|---|
| `portfolio.db` | 6 | advices 1229、portfolios 1、positions 3、simulations 5、transactions 11、watchlist 22 |
| `job_runs.db` | 13 | job_runs 1268、job_plan 20、task_run_events 4925 |
| `notification_outbox.db` | 1 | notification_outbox 48 |
| `business.db` | 58 | accounts 1、portfolios 1、position_cycles 3、executions 11、watch_subscriptions 22、advices 1209、business_job_runs 15 |
| `management.db` | 29 | job_runs 1301、dataset_current 4700、dataset_versions 9538、instruments 6915 |
| `warehouse/meta.db` | 9 | instruments 6915、fundamental_manifest 4574、daily_manifest 37 |

### 5.2 `portfolio.db` 到 `business.db`

当前 `business.db.legacy_entity_map` 有 1251 条旧实体映射：

| 旧类型 | 分类 | 数量 |
|---|---|---:|
| portfolio | migrated | 1 |
| position | reconstructed | 3 |
| transaction | reconstructed | 11 |
| watchlist | reconstructed | 22 |
| advice | reconstructed | 1209 |
| simulation | unknown | 5 |

结论：

- 组合、持仓、交易和关注关系已有对应新库记录或映射；
- 建议记录主要是 `reconstructed`，不是逐字段无损迁移证明；
- 旧模拟记录为 `unknown`，不能证明已迁移为新 `SimulationRun/Fill/Result`；
- 当前不能删除 `portfolio.db`。

### 5.3 `notification_outbox.db`

该库仍有 48 条 `notification_outbox` 记录，当前没有证明这些记录已完整迁移到 `business.db` 的对账结果。

结论：

```text
notification_outbox.db：未完成迁移审计，不能删除
```

### 5.4 `job_runs.db`

`job_runs.db` 仍有 1268 条 `job_runs` 和 20 条 `job_plan` 记录。虽然已有向 `management.db` 导入和 `record_origin` 机制，但本次未完成全部历史记录逐条对账、重复冲突处理和只读切换验证。

结论：

```text
job_runs.db：存在迁移能力，但未完成全量对账和归档，不能删除
```

### 5.5 `warehouse/meta.db`

截至本报告日期，旧元数据库仍有 6915 条 instruments 和 4574 条 fundamental manifest 等历史元数据。当前生产默认管理库为 `management.db`；旧库仅应保留为显式迁移/备份/归档输入，生产运行时引用收口需以最新审计结果为准。

结论：

```text
meta.db：生产主路径已绕开，但未完成引用清理和删除前验证，不能删除
```

### 5.6 历史 Factors

当前仍存在：

```text
文件数：13
总大小：约 245 KB
范围：2025-08 至 2026-08
```

Factors 正式 consumer 已移除，但历史文件不能直接视为已迁移到 indicators，也不能在没有业务等价性和备份确认的情况下删除。

## 6. 当前完成度

| 项目 | 状态 | 说明 |
|---|---|---|
| 生产 `stock_daily/2026-08` 路径修复 | 已完成 | Current、路径和 checksum 一致 |
| 全部 Current 文件校验 | 已完成 | 4700 条 Current 全部通过 |
| 业务数据读取恢复 | 已完成 | 有限范围 signal_scan 冒烟通过 |
| 测试数据库隔离 | 已完成 | 生产环境变量不会污染临时测试库 |
| Business Worker | 已接入 | 容器健康，队列可消费 |
| Business Scheduler | 已接入 | 当前只调度观察过期维护任务 |
| 业务任务基础可靠性 | 基本完成 | 相关定向测试和生产验证通过 |
| Screen/Research 失败落库 | 基本完成 | 仍需更完整的 orphan 查询策略 |
| `DataContext` | 基础完成 | 底层兼容字段仍保留 |
| `signal_scan` | 已实现 | 已通过单测和有限真实数据验证，尚无全市场性能基线 |
| Factors 退役 | 部分完成 | 正式 consumer 已移除，历史文件仍保留 |
| `job_runs.db` 迁移 | 部分完成 | 有导入能力，未完成全量对账/只读归档 |
| `meta.db` 下线 | 未完成 | 仍有回退、测试、迁移和旧读取引用 |
| `portfolio.db` 迁移 | 未完成 | 存在 reconstructed/unknown 记录 |
| `notification_outbox.db` 迁移 | 未确认 | 仍有独立记录 |

## 7. 当前风险

### 高风险

1. 旧业务库删除会导致历史建议、模拟或通知事实不可恢复。
2. `portfolio.db` 的 reconstructed/unknown 数据不适合作为无损迁移依据。
3. 任何绕过测试 fixture 的临时库初始化都可能重新污染生产管理元数据，必须显式传入临时数据库路径。

### 中风险

1. `DataContext` 底层仍有兼容字段，后续模块可能继续依赖旧字段。
2. `signal_scan` 尚未完成全市场性能、内存和复杂条件验证。
3. 业务 Scheduler 当前只调度一个维护任务，建议和通知任务不会自动刷新。

## 8. 删除结论

当前不满足 D 范围删除条件。

明确结论：

```text
portfolio.db                 不可删除
notification_outbox.db       不可删除
job_runs.db                  不可删除
warehouse/meta.db            不可删除
warehouse/factors/*.parquet  暂不删除
旧 RuleContext/Fetcher 代码  暂不删除
```

后续删除前必须再次完成：

```text
全量迁移对账
→ 备份恢复验证
→ 运行时引用清理
→ 隔离环境删除冷启动
→ 全量测试
→ 生产只读观察
→ 用户再次确认
```

## 9. 下一步建议

当前业务已经恢复，不建议继续扩大改动。后续只需按优先级处理：

1. 对 `management.db` 增加 Published Path/checksum 的健康诊断，防止元数据污染再次影响业务。
2. 完成 `portfolio.db`、`notification_outbox.db` 的只读迁移对账。
3. 完成 `job_runs.db`、`meta.db` 的只读归档验证。
4. 再决定是否删除旧库和历史 Factors 文件。
