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

## Current Verified State

- `Warehouse` 默认使用 `output/data/management.db`；生产默认路径不再回退到旧元数据库。
- `DatasetAccess` 是正式数据读取入口，读取 `dataset_current` 并校验 Published 状态、文件存在性、质量和 checksum。
- 行业成员、行业日线、行业特征和行业轮动已接入 Published 数据链路。
- 生产 `management.db` 已存在并启用 stock_daily、industry、指标和行业相关任务定义。
- 旧库仅允许作为显式迁移、备份、诊断或测试输入，不能作为生产业务读取源。

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
