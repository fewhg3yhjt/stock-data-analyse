# 数据链路与产品状态流转收口实施任务书

> 面向后续 AI/开发者的可执行改造文档。本文针对当前工作树审查结果制定，不要求重写现有系统；应按阶段实施、验证并独立提交。

文档性质：实施计划，不代表工作项已完成。每一阶段还必须记录代码状态、测试状态、生产接入状态、实际提交和残余风险。

当前进度说明：本文列出的阶段大多仍是待办或部分完成；单实例 `business-worker` 和业务 Scheduler 已接入生产，业务 Scheduler 当前仅调度观察过期维护任务；旧库下线和跨入口验收尚未闭环。

## 1. 背景与结论

当前项目已经具备以下目标链路的大部分组件：

```text
External Source
→ Immutable Raw Batch
→ Candidate Build
→ Quality Check
→ Published Version
→ Dataset Current
→ Indicators（研究因子统一归入指标；历史 Factors 仅归档）
→ Business Consumers
```

但实际运行仍是“新版治理链路 + 旧版业务数据面”双轨并存，尚未形成完整闭环。主要断点包括：

1. 任务中心显示启用不代表 APScheduler 已注册。
2. 质量失败可能被任务层记录为成功，并继续进入发布阶段。
3. Publish 可能在缺少本次输出版本时选择历史合格 Candidate。
4. Indicators 未经过独立质量检查即登记 `PASS/published`；历史 Factors 文件和诊断兼容代码仍保留，已从 `stock_daily` 正式 consumer 和管理库健康统计中移除。
5. `management.db` 与历史旧库之间的事实对账和运行时隔离仍需完成。
6. 生产文件、数据集配置和消费者之间存在字段契约不一致。
7. 大量正式业务消费者绕过 `DatasetAccess`，直接读取物理文件或静默在线回退。
8. 日期口径、数据来源、降级状态和质量状态没有贯穿到 API 和 UI。
9. 后台任务缺少跨入口互斥、可靠提交和进程重启恢复。
10. 数据中心的健康状态不能真实表达全市场覆盖和业务可用性。

补充的业务事实缺口：新 `biz` 模拟链路会在内存生成 `SimulationEvent`，但当前没有 `biz/repo.py` 写入/查询方法，`simulation_events` 表不会获得新链路事件。旧 backend domain service 的事件写入测试不等价于新 `biz` 链路验收。

补充的契约与迁移缺口：`DataContext` 文档字段与 `biz/models.py`、`DatasetAccess` 输出尚未完全一致；旧 `RuleContext` 仍被 `strategy/core/portfolio` 正式旧链路使用；数据源统一只完成新 `biz` 路径，旧 Fetcher/fallback 仍有调用方；Factors 目标已废弃但配置、术语和历史兼容链路仍未完全退役。

改造目标是收口事实源、状态传播和消费契约，不是推倒重来。

## 2. 最终目标

### 2.1 数据生成约束

1. Raw 采集成功前，不得将正式业务数据标记为可用。
2. Build 必须绑定明确的 Source Batch、请求范围和输入版本。
3. Quality `FAIL` 必须阻断当前 Candidate 发布。
4. Publish 只能发布当前请求显式指定且通过质量检查的版本。
5. Indicators 和 Factors 必须有独立质量门禁，不得自动登记 `PASS`。
6. 空数据和严重低覆盖数据不得覆盖正式分区。
7. 业务消费者默认只读取 Published Dataset。
8. 允许降级的研究场景必须显式返回来源、日期和降级原因。
9. 正式建议、模拟和建仓不得静默使用低质量或陈旧数据。

### 2.2 任务状态约束

目标链路：

```text
Task Definition
→ Execution Request
→ Job Run
→ Source Batch / Candidate / Artifact
→ Terminal Result
```

要求：

1. 状态转换必须有合法性校验。
2. 终态不得原地恢复为 `running`，Retry 必须创建新 Run。
3. `partial_success` 是否继续由任务策略决定。
4. Request、Run、Batch、Version 和 Artifact 必须可相互追溯。
5. 容器重启后遗留 `running` 必须自动回收。
6. 手工执行、Retry 和 Scheduler 必须共用同一套互斥机制。

### 2.3 产品消费约束

目标链路：

```text
Published Dataset
→ Unified Data Access
→ Business Service
→ API DTO
→ UI
```

正式业务响应至少应能表达：

```text
data_as_of
source
fallback_used
fallback_reason
quality_status
dataset_version
is_stale
```

## 3. 实施原则

1. 严格按阶段实施，每个阶段单独验证和提交。
2. 先修正确性，再迁移消费入口，最后清理旧路径。
3. 不一次性重构所有业务模块。
4. 新路径稳定前，不删除旧数据库、旧 Parquet 或历史文件。
5. 禁止直接修改生产数据状态为 `PASS`，禁止清空或覆盖历史数据。
6. 数据库结构变更只允许向前兼容、幂等的增量迁移。
7. 不通过捕获所有异常并返回空数据掩盖契约错误。
8. 正式业务不得为“保持可用”而恢复静默 fallback。
9. 当前仓库即生产部署目录；纯代码变更通常只需重启 `stock-web`，不要无条件 rebuild。
10. 提交时仅暂存本阶段文件，不得夹带工作区已有的无关修改。

## 4. 阶段一：配置与诊断基线

### 目标

确保所有任务和数据集配置可解析，并建立不修改生产数据的契约诊断能力。

### 重点文件

- `config/datasets/stock_daily.yaml`
- `config/datasets/indicators.yaml`
- `config/datasets/factors.yaml`
- `config/tasks/*.yaml`
- `warehouse/dataset_config.py`
- `tests/test_dataset_metadata.py`

### 工作项

1. 修复全部 YAML 语法问题，特别检查 `stock_daily.yaml` 字段映射缩进。
2. 增加配置全集测试，遍历加载 `config/datasets/*.yaml` 和 `config/tasks/*.yaml`。
3. 加载失败时报告具体文件和解析错误，不得静默跳过。
4. 增加只读数据契约诊断，输出：

```text
dataset
partition
row_count
symbol_count
min_date
max_date
columns
current_version
quality_status
publish_status
checksum_match
```

5. 固化低覆盖异常测试：日线数千标的而 Factors 仅少量标的时，不得判定健康。
6. 诊断 Published Path 在当前环境是否可达，并明确报告容器绝对路径问题。

### 验收

```text
pytest tests/test_all_configs.py
pytest tests/test_dataset_metadata.py
```

必须满足：

1. 全部配置可解析。
2. 新增配置文件会自动进入测试范围。
3. 诊断能够识别 Factors 严重低覆盖。
4. 诊断不修改生产数据库或文件。
5. 本阶段不改变调度和业务消费行为。

### 建议提交

```text
fix: validate all task and dataset configs
```

## 5. 阶段二：修复质量结果与任务状态传播

### 目标

彻底阻止以下错误流转：

```text
Quality FAIL
→ Job success
→ Pipeline Publish
```

### 重点文件

- `ops/job_runs.py`
- `ops/task_runner.py`
- `ops/task_execution.py`
- `warehouse/quality.py`
- `warehouse/publish.py`
- `tests/test_task_runner.py`
- `tests/test_pipeline_stages.py`

### 工作项

1. 统一 Worker Result 契约，至少包含：

```text
status
ok
rows
failed_count
publish_allowed
output_versions
error
```

2. 显式 `status` 为首要判定依据；只有旧 Worker 没有显式状态时才允许兼容推断。
3. `ok=False` 必须映射为 `failed`。
4. `publish_allowed=False` 不得映射为 `success`。
5. `rows > 0` 只能表示生成了检查报告，不能代表质量成功。
6. Quality 结果按以下语义推进：

```text
PASS    → success，允许发布
WARNING → 按配置决定是否允许发布，但不得伪装为 PASS
FAIL    → failed，禁止发布
```

7. `execute_pipeline()` 同时检查运行状态、业务结果和任务策略。
8. 删除 Publish 的隐式历史候选补位逻辑。Publish 没有明确 `output_versions` 时必须失败。
9. 发布前校验 Candidate Version、Quality Version 和 Requested Version 完全一致。
10. 人工发布历史 Candidate 应使用独立显式入口，不得复用自动流水线回退。
11. `expected_symbols` 必须来自请求固化 Universe、UniverseSnapshot、active instruments 或明确配置基准，禁止使用 Candidate 自身 `symbol_count`。
12. Publisher 必须按 `dataset_name + partition_key` 获取数据库租约或等价分区锁；锁覆盖读取 current、复制 rollback、标记 publishing、替换文件、更新 current 的完整发布临界区。
13. 模拟任务必须持久化 `SimulationEvent`，并与 `SimulationRun`、`SimulationFill`、`SimulationResult` 保持相同 run 关联；正常路径和模拟失败基础路径已实现，Screen/Research 前置事实的 orphan/reconciled 策略仍需完善。
14. `DataContext` 必须形成单一 canonical DTO；`partition_versions`、`max_date` 等内部字段不得直接作为新业务公共契约。
15. 新 `biz` 统一使用 `StrategyContext`；旧 `RuleContext` 只能存在于明确标记的迁移兼容链路，迁移完成前不得删除。
16. Factors 退役必须停止新生产、清理 active 配置/任务/消费者/术语，并将历史文件转为显式 legacy archive；不得误删历史文件。

### 必测场景

1. Quality 返回 `FAIL` 且 `rows > 0`，Job Run 为 `failed`。
2. Quality `FAIL` 后 Publish 不执行。
3. 数据库存在历史 PASS Candidate 时，也不得在本次失败后发布历史版本。
4. WARNING 允许发布和禁止发布两种策略均有测试。
5. Publish 缺少明确版本时失败。
6. Publish Version 与 Quality Version 不一致时失败。
7. Request 和 Job Run 终态一致。
8. Candidate 自身数量变化不能改变 coverage 基准；缺失 Universe 标的时必须降低质量或阻断发布。
9. 两个并发 Publisher 发布同一分区时只有一个进入临界区，最终文件、Current 指针和版本状态一致。
10. 新 `biz` 模拟链路产生的 `DATA_GAP`、`FILLED`、`ORDER_REJECTED`、`END_OF_PERIOD` 等事件可从业务库按 run 查询。
11. DatasetAccess、Screen、Research、Simulation 和 API DTO 返回一致的 DataContext 字段与版本引用。
12. 旧 `RuleContext` 仅由迁移兼容链路引用，且有迁移前后回归对账。
13. Factors 不再被新任务、API、数据中心或消费者作为正式结果依赖，历史归档可被显式查询。
14. Screen/Research/Simulation 任务失败后均有可查询领域终态；无对应成功运行的前置版本和 Universe 事实不会被展示为成功结果。

### 验收

```text
pytest tests/test_task_runner.py
pytest tests/test_pipeline_stages.py
pytest tests/test_dataset_access.py
```

### 建议提交

```text
fix: enforce pipeline quality outcomes
```

## 6. 阶段三：真实覆盖率与派生数据质量

### 目标

让 `PASS`、`WARNING` 和 `healthy` 表达真实业务可用性，而不是文件内部自证。

### 重点文件

- `warehouse/quality.py`
- `warehouse/pipeline_state.py`
- `warehouse/indicators_build.py`
- `warehouse/factors.py`
- `ops/data_center_service.py`
- `ops/management_db.py`
- `config/datasets/*.yaml`

### 工作项

1. `expected_symbols` 不得来自 Candidate 自身。
2. 执行请求固化 `symbols`、`symbol_count` 和 `universe_fingerprint`。
3. 质量基准优先级：

```text
请求固化 symbols
→ 请求固化 universe snapshot
→ 同业务日期 active instruments
→ 明确配置基准
```

4. 增加空数据保护：`row_count=0`、`symbol_count=0`、主键缺失、关键字段全空均为 `FAIL`。
5. Publisher 增加第二道空文件保护。
6. 执行数据集 nullable 约束，至少检查 `date`、`code`、`close`、`volume`、`amount`。
7. 增加日期覆盖：`expected_trade_date`、`symbols_with_expected_date`、`date_coverage`、`stale_symbols`。
8. 明确停牌证券处理策略，不得简单将所有证券都要求有当日行情。
9. Indicators 独立检查代码覆盖、日期范围、核心指标非空率、输入版本和失败证券。
10. 历史 Factors 不再进入新的独立质量链路；新研究因子统一由 Indicators 质量门禁覆盖。
11. 移除派生版本自动登记 `PASS/published` 的行为。
12. 数据中心健康基准使用有效 Universe 或上游 Published Dataset，禁止 `8/8=100%` 式自证。

建议健康状态：

```text
healthy  = 质量通过且日期满足
partial  = 有数据但覆盖不足
stale    = 覆盖满足但日期滞后
critical = 质量失败或严重缺失
empty    = 无有效数据
unknown  = 无法建立基准
```

### 必测场景

1. 空 Candidate 必须 `FAIL`。
2. `500/6800` 不得得到 100% 覆盖。
3. `8/6800` 的 Factors 必须为 `critical` 或 `FAIL`。
4. 必填字段为空必须 `FAIL`。
5. 单证券计算失败时，派生数据不得无条件 `PASS`。
6. 输入版本必须写入派生版本。
7. 日期滞后和覆盖不足能够区分。
8. Publisher 拒绝空文件及无质量结果版本。

### 建议提交

```text
fix: enforce dataset coverage quality
```

## 7. 阶段四：统一 Scheduler 与任务配置事实源

### 目标

使任务中心、Active Config 和 APScheduler 注册状态一致。

### 唯一事实源

```text
YAML = 初始定义和默认配置
management.db active config = 运行时唯一事实源
APScheduler = active config 的运行时投影
```

### 重点文件

- `ops/task_center.py`
- `ops/task_center_service.py`
- `web/scheduler.py`
- `web/app.py`
- `config/tasks/*.yaml`

### 工作项

1. Scheduler 不再直接以当前 YAML 的 `schedule.enabled` 作为运行事实。
2. Scheduler 从 `task_definitions` 和 Active Config 读取任务和调度参数。
3. 只有任务定义启用且 Active Config 调度有效时才注册。
4. Job ID 统一为 `task:<task_key>`。
5. 配置启用、停用或激活后，真正重载数据任务：删除现有 `task:*`，再按 Active Config 注册。
6. 重载不得影响 Outbox、分钟快照和通知 Trigger。
7. 重载失败时 API 不得只返回“保存成功”。
8. `WAREHOUSE_DAILY_SYNC` 降级为 Legacy 状态或迁移提示，不再作为新版调度事实源。
9. `/api/data/scheduler` 明确返回：configured、enabled、registered、effective、next_run、reason。
10. 避免 Capture、Build、Quality、Publish 分别依赖时间碰撞；建议只定时注册顶层 Pipeline，由它串行执行各阶段。

### 必测场景

1. YAML 修改但未激活，不改变 Scheduler。
2. 激活后 Job 时间更新。
3. 停用后 Job 被移除，启用后 Job 被创建。
4. 重载不影响非数据任务。
5. 容器重启后按数据库 Active Config 恢复。
6. 同一任务不会重复注册。

### 建议提交

```text
fix: use active task configs for scheduling
```

## 8. 阶段五：任务互斥、幂等与重启恢复

### 目标

保证手工执行、Retry、Scheduler 和容器重启场景不会重复写入或永久卡死。

### 重点文件

- `ops/task_center.py`
- `ops/task_runner.py`
- `ops/task_execution.py`
- `ops/job_runs.py`
- `warehouse/source_batches.py`
- `web/app.py`
- `web/scheduler.py`

### 工作项

1. HTTP 返回 `202` 前先持久化 Request 和 Run。
2. 响应返回 `request_id`、`run_id`、`task_key`、`status_url`。
3. 建立幂等指纹：

```text
task_key
period_start
period_end
universe_fingerprint
config_version
trigger_scope
```

4. 增加 SQLite 任务锁表，建议字段：

```text
lock_key
owner_run_id
acquired_at
heartbeat_at
expires_at
```

5. 获取锁必须为数据库原子操作；手工、Retry 和 Scheduler 共用。
6. 锁至少覆盖同任务、同分区和同数据集写入组。
7. 应用启动时回收遗留 Request、Job Run、Source Batch、任务锁和 `publishing` 版本。
8. 回收错误使用明确原因，如 `process_restarted` 或 `stale_run_reclaimed`。
8.1. 所有 heartbeat 时间必须使用同一可比较格式；不得将 RFC3339 `T...Z` 字符串直接与 SQLite `datetime()` 的空格格式做 TEXT 字典序比较。stale 回收必须通过时间解析或统一 UTC 存储格式验证。
9. Retry 复用原请求的日期、Symbols、Universe、Config Version 和 Input Versions。
10. Retry 记录 `retry_of_request_id`、`retry_of_run_id` 和 `attempt`。
11. 真正执行 `retry_limit`、`on_partial_success` 和 `on_failure`。
12. Retry 改为后台执行，不得同步占用 Waitress 请求线程却返回 `202`。
13. `run_next()` 必须在同一数据库事务内原子领取 requested Run；领取成功后才能进入执行，竞争失败不得伪装成 500 任务失败。

### 必测场景

1. 两个并发请求只有一个获得相同锁。
2. Scheduler 与手工 API 同时触发只运行一个。
3. 重启后遗留 Job Run 和 Source Batch 被回收。
4. Retry 使用原 Symbols 和 Config Version。
5. 自动重试创建新 Run，不覆盖旧 Run。
6. 达到 Retry Limit 后停止。
7. `partial_success=block_downstream` 时阻断下游。
8. HTTP `202` 返回前 Request 已存在。
9. 最近五分钟内的 RFC3339 heartbeat 不被回收，超过租约的业务 Run 可以被回收。
10. 两个并发 Worker 领取同一 requested Run 时只有一个成功 claim，另一个得到可重试的竞争结果，不能执行同一 Run 两次；当前已通过事务内 claim 实现，需补完整并发回归。

### 建议提交

```text
fix: make data task execution recoverable
```

## 9. 阶段六：统一数据库职责

### 目标职责

```text
management.db
  → task definitions/configs/requests/runs/events
  → source batches
  → dataset versions/current/quality/health
  → artifacts/lineage
  → instruments

portfolio.db
  → watchlist/positions/transactions/cash

notification_outbox.db
  → notification delivery queue

Parquet / CSV
  → analytical datasets
```

历史旧库文件进入只读归档输入阶段，不再接收生产运行时新事实。

### 9.1 `warehouse/meta.db` 下线专项

当前生产配置已通过 `MANAGEMENT_DB_PATH` 使用 `management.db`，但旧库名称仍出现在显式迁移、测试、备份和历史文档中，生产入口全量审计尚未完成。旧库属于历史归档输入，不能在当前阶段直接删除。删除前必须完成：

1. 全量扫描并清理生产代码对旧元数据库的隐式默认和业务读取引用。
2. 将 instruments、manifest、行业及其他仍有价值的事实迁移到目标管理库，并输出数量、代码和版本差异报告。
3. 测试、Shadow、迁移工具改为显式注入目标管理库或显式只读归档输入。
4. 在隔离目录执行冷启动、全量回归、数据访问、任务调度和健康检查验证。
5. 在生产完成只读观察，确认 `management.db` 能独立承载数据引擎和下游访问。
6. 完成备份和归档后人工确认，先禁止运行时创建，再另行执行删除。

### 工作项

1. 禁止任务执行中临时修改 `warehouse.meta_db_path`，改为构造时显式注入。
2. 对账并补齐 `management.db` 所需 Schema。
3. 评估 `daily_manifest`、`factor_manifest` 是否可由 `dataset_versions` 替代；不能立即移除时先迁移兼容。
4. 将完整 Instruments 幂等迁移到 `management.db`，处理 Code、Name、Industry、Status、Source、Updated At。
5. 输出迁移前后数量和代码差异，不覆盖更新更晚的数据。
6. 迁移有运维价值的旧 Job Runs，保留 Legacy ID 映射并处理重复记录。
7. 所有新 API、Scheduler 和任务只读取 `management.db`。
8. 增加 `schema_migrations` 表和正式增量迁移机制。
9. 每个迁移只执行一次，执行前后运行 SQLite `quick_check`。
10. 使用旧 Schema Fixture 验证升级幂等和数据不丢失。

### `job_runs.db` 收敛专项

`job_runs.db` 与 `management.db` 均曾承载 TaskCenter 相关表，不能仅通过将默认路径改为 `management.db` 视为完成迁移。收敛必须单独完成：

1. 列出 `job_runs.db` 中仍有价值的 `job_runs`、`job_plan`、任务定义、配置、事件和指标管理事实，并建立旧 ID 到 `management.db` ID 的映射。
2. 对 `job_runs.db` 与 `management.db` 做任务定义、运行记录、计划和状态数量/内容对账，明确重复记录、冲突状态和保留策略。
3. 将需要保留的历史运行记录以只读迁移方式导入 `management.db`，标记 `record_origin=legacy`，不得覆盖更新较新的新事实。
4. 统一所有 API、Scheduler、Runner、数据中心和恢复逻辑的默认路径为 `management.db`，禁止运行时无提示回退到 `job_runs.db`。
5. 将 `job_runs.db` 改为只读归档输入，增加写入拦截或启动诊断，确认运行期间不会产生新事实。
6. 在隔离环境移除 `job_runs.db` 后完成应用冷启动、任务中心查询、调度注册、手工执行、失败恢复和健康检查。

验收：新运行只写 `management.db`；历史记录可通过 Legacy ID 映射查询；`job_runs.db` 只读且不再产生新记录；两个数据库的差异有报告和处理结论。

### 验收

1. 新运行事实只写入 `management.db`。
2. Instruments 与任务 Universe 一致。
3. 不再通过运行时修改对象路径切换数据库。
4. 数据中心、任务中心和健康 API 使用同一事实库。
5. Legacy 数据库保留但不再写入。

### 建议提交

```text
refactor: unify warehouse management metadata
```

## 10. 阶段七：统一字段与 Schema 契约

### 目标

生产文件、数据集 YAML、统一访问层和消费者使用同一字段体系。

建议统一为 snake_case：

```text
date, code, open, high, low, close, pre_close,
volume, amount, turn, trade_status,
pe_ttm, pb_mrq,
ma5, ma10, ma20, ma60, volatility_20
```

### 设计决策

推荐将估值拆为：

```text
stock_daily = 行情事实
valuation_daily = PE/PB 等估值事实
```

短期无法拆分时，可在 Stock Daily 保留 `pe_ttm/pb_mrq`，但配置、文件和消费者必须统一命名。

### 工作项

1. 提升存在破坏性字段变化的数据集 `schema_version`。
2. `WarehouseSource` 只查询真实存在的标准字段。
3. 可选字段缺失时补空列并记录 Schema Mismatch；必填字段缺失时抛明确契约错误。
4. 禁止吞掉所有 SQL 异常后返回空表。
5. Indicators 新结果只写标准小写字段。
6. 旧 `MA20/volatility20` 在统一访问层标准化，页面不再分别兼容。
7. Factors 新生产链路按退役流程清理：停止任务和调度、移除 active 配置和消费者、清理数据中心/健康检查/API/术语残留；历史 Factors 文件只作为 legacy archive 保留。
8. 估值输入不可用时，相关指标或历史因子归档状态标记为 unavailable，并在质量报告中列出缺失依赖。
9. 为 WarehouseSource、DatasetAccess、Builders 和主要消费者增加字段契约测试；不得让新消费者依赖 Factors。

### 验收

1. 日线查询不再因 `peTTM/pbMRQ` 缺失整体失败。
2. 新数据仅使用标准字段。
3. 旧指标分区可通过统一访问层读取。
4. 页面不再直接判断大小写字段名。
5. Factors 配置字段与 Parquet 字段一致。
6. Schema Mismatch 可观测且不会伪装为在线源正常返回。
7. 新任务、API、数据中心和消费者不再创建或依赖 Factors 正式结果；历史 Factors 仅可通过显式归档入口访问。

### 建议提交

```text
fix: align warehouse dataset schemas
```

## 11. 阶段八：业务消费者接入 Published Dataset

### 迁移顺序

1. `portfolio/monitor.py`
2. `market_discovery/service.py`
3. `strategy_lab.py`
4. `portfolio/dashboard.py`
5. `core/engine.py`
6. 收益分析与晨报
7. CLI Scanner 和导出

每个消费者单独迁移、测试和提交。

### 统一 Data Context

访问层返回 DataFrame 的同时返回：

```text
dataset_name
dataset_version
quality_status
data_as_of
source
fallback_used
fallback_reason
is_stale
```

业务服务不得丢弃该上下文。

### 消费策略

| 消费场景 | 最低质量 | 在线回退 | 陈旧数据 |
|---|---|---:|---:|
| 市场发现 | PASS | 否 | 否 |
| 持仓建议 | PASS | 否 | 否 |
| 模拟建仓 | PASS | 否 | 否 |
| 正式建仓 | PASS | 否 | 否 |
| 策略回测 | PASS | 否 | 允许指定历史日期 |
| 个股研究 | WARNING | 是 | 允许但必须提示 |
| 页面预览 | WARNING | 是 | 允许但必须提示 |
| 晨报 | PASS/WARNING | 可配置 | 必须标明日期 |

### 工作项

1. Portfolio Advice 默认读取 Published Dataset，禁止正式建议静默在线回退。
2. 日线技术数据和实时价格分别标注来源及时间。
3. 建议结果保存输入 Dataset Version 和 `data_as_of`。
4. Market Discovery 使用 Published 日线或 Factors，并采用统一全局截止日期。
5. 停牌证券明确返回证券自身数据日期。
6. 只有实际查询到的字段才能作为筛选和排序条件。
7. Strategy Lab 读取 Published Dataset，严格裁剪回测区间并返回输入版本。
8. Stock Detail 区分 `latest_close`、`realtime_price`、`daily_as_of` 和 `realtime_as_of`。
9. 实时行情失败时回退最近收盘价，并标记 `price_type=close_fallback`。
10. 所有缓存返回前强制执行请求日期范围裁剪，消除未来数据泄漏。
11. 模拟、建仓和建议 API 在服务端校验质量、新鲜度和必需字段。

### API 目标示例

```json
{
  "data": {},
  "data_context": {
    "data_as_of": "YYYY-MM-DD",
    "source": "published_dataset",
    "fallback_used": false,
    "fallback_reason": null,
    "quality_status": "PASS",
    "dataset_version": "...",
    "is_stale": false
  }
}
```

### 必测场景

1. Candidate 对业务不可见，Published PASS 可见。
2. Quality FAIL 数据不能用于正式建议。
3. 缺少 Published 数据时正式建议明确失败，不静默回退。
4. 研究页面允许回退时返回 `fallback_used=true`。
5. 缓存包含未来数据时严格裁剪。
6. Market Discovery 使用统一截止日期。
7. 实时失败时回退最近收盘价。
8. 直接调用模拟或建仓 API 也不能绕过质量门禁。

### 建议分拆提交

```text
refactor: use published data for portfolio advice
refactor: use published data for market discovery
refactor: use published data for strategy lab
refactor: expose data context in stock detail
```

## 12. 阶段九：统一交易日期与 Freshness

### 目标

全系统使用同一个“最新已收盘交易日”入口。

### 工作项

1. 提供统一交易日服务：

```text
latest_closed_trade_day(now)
previous_trade_day(date)
is_trade_day(date)
expected_trade_day_for_job(run_time)
```

2. 使用 `Asia/Shanghai` 时区和可靠交易日历。
3. 暂时无法接入完整交易日历时，至少统一周末处理并维护可配置节假日表。
4. 移除各模块重复的 `today - timedelta(days=1)`。
5. 交易日 15:35 运行时使用当天；非交易日使用最近交易日。
6. Freshness 基于 `expected_trade_day`、`actual_data_as_of`、Quality 和 Coverage。
7. Freshness 使用新版 Task 和 Dataset Current，不再依赖旧任务名。
8. 页面分别展示日线截至、实时行情截至、指标截至和建议生成时间。
9. 未真实接入当日实时源时，禁止使用“现价盘中实时”文案。

### 必测场景

1. 周一 15:35 返回周一，不是周日。
2. 周末和节假日返回最近交易日。
3. 交易日盘前返回上一交易日，收盘后返回当天。
4. Freshness 使用 Dataset Current 和新任务名。
5. Stale 数据不能用于正式建议。

### 建议提交

```text
fix: unify trading date and freshness rules
```

## 13. 阶段十：发布一致性与恢复

### 目标

降低文件系统与 SQLite 无法跨资源原子提交造成的版本分裂风险。

### 工作项

1. 发布前记录完整意图：Version、Target、Candidate、Checksums、Previous Version 和 Started At。
2. 启动时扫描 `publishing`、`publish_failed`、Current Checksum Mismatch 和 Published File Mismatch。
3. 恢复规则：

```text
正式文件 checksum = 新版本 → 补齐 DB Current
正式文件 checksum = 旧版本 → 回退新版本状态
两者都不匹配 → 转人工处理，不自动选择其他版本
```

4. 所有补偿操作记录事件并保持幂等。
5. `published_path` 改为相对 Warehouse Root 的路径，运行时解析。
6. 历史绝对路径通过显式迁移处理，不长期保留多路径猜测。
7. Publish 必须按 `dataset_name + partition_key` 获取数据库租约或等价分区锁；锁覆盖读取 current、复制 rollback、标记 publishing、替换文件、更新 current 的完整临界区。

### 必测场景

1. 模拟文件替换后、数据库更新前崩溃。
2. 启动恢复能补齐 Current。
3. 文件仍为旧版本时恢复正确。
4. 文件与新旧 Checksums 都不一致时不自动发布。
5. 容器和宿主机都能解析相对路径。
6. 两个 Publisher 并发发布同一分区时只有一个进入临界区，最终文件、Current 指针、版本状态和 previous_version 关系一致。

### 建议提交

```text
fix: recover interrupted dataset publishing
```

## 14. 阶段十一：产品状态机收口

### 工作项

1. 集中定义 Request、Job Run、Source Batch、Dataset Version、Outbox 和 Analysis Task 的状态与合法转换。
2. 状态更新必须检查 Current Status、Target Status 和 Allowed Transition。
3. 默认禁止终态原地转回 `running`。
4. Retry 创建新 Request/Run，不修改旧终态。
5. 若保留 `cancelled`，必须实现 Cancel API、Cancel Requested、Worker 检查、安全中断和状态同步；否则先移除不可达的取消操作和文案。
6. 后端统一输出状态 Code、Label、Severity 和 Terminal。
7. 前端不再维护多套不完整状态映射。
8. 启用状态、Scheduler 状态和最近运行状态分开表达。
9. Web Analysis 和 Comparison 任务状态持久化，返回稳定 Task ID 和查询 API。
10. 暂不引入持久 Worker 时，至少在重启后将中断分析标记失败，不能无记录消失。

### 必测场景

1. Success Run 不能重新变成 Running。
2. Retry 创建新 Run。
3. 非法状态转换失败。
4. Cancel 能力真实可达，或完全不暴露。
5. 任务停用但历史成功时，同时显示“已停用”和“上次成功”。
6. 分析任务重启后有明确终态。

### 建议提交

```text
refactor: enforce product state transitions
```

## 15. 阶段十二：运维可靠性与可观测性

### 工作项

1. Outbox 增加原子 Claim/Lease：`pending → processing → sent/dead`。
2. 增加 `claimed_by`、`claimed_at`、`lease_expires_at`，避免即时线程和 Scheduler 重复发送。
3. 正常生产 Worker 接入 `register_artifact()` 和 `link_lineage()`。
4. 能够从 Run 追踪文件，从 Published Version 追踪 Raw Batch 和输入版本。
5. 收益、持仓和组合导出增加行情日期、来源、版本、质量和失败证券。
6. 失败证券不得从结果和导出中静默消失。
7. 备份覆盖：

```text
portfolio.db
management.db
notification_outbox.db
warehouse/
config/
custom schemes and notification config
```

8. SQLite 使用安全备份方式，恢复后执行 `quick_check`、临时应用启动和关键数据查询。

### 建议提交

```text
fix: improve operational reliability
```

## 16. 端到端测试矩阵

### 16.1 配置 E2E

```text
加载全部 YAML
→ 初始化 Management DB
→ Seed Definitions
→ 激活配置
→ Scheduler 注册
```

### 16.2 数据流水线 E2E

```text
Fake External Source
→ Capture
→ Raw Batch
→ Build
→ Candidate
→ Quality
→ Publish
→ Dataset Current
→ Indicators
→ Factors
→ Business Access
```

断言 Request/Run/Batch 状态、版本传递、质量阻断、Artifact/Lineage、Current 指针和业务 Published 读取全部正确。

### 16.3 重启恢复 E2E

```text
创建 running task/source batch/publishing version
→ 模拟进程退出
→ 重新初始化应用
→ 验证回收与发布补偿
```

### 16.4 下游消费 E2E

```text
Published PASS Daily
→ Portfolio Advice
→ Market Discovery
→ Strategy Lab
→ Stock Detail API
```

断言返回 Version、Data As Of、Source、Quality，且无静默 Fallback。

### 16.5 Compose E2E

```text
docker compose up
→ Waitress 启动
→ Scheduler 初始化
→ Healthcheck
→ 登录和关键 API
→ Scheduler Job 状态
```

### 16.6 备份恢复 E2E

```text
生成备份
→ 恢复到临时目录
→ SQLite quick_check
→ 启动临时应用
→ 查询持仓、任务、版本和 Outbox
```

## 17. 强制实施顺序

1. 配置与诊断基线。
2. 质量结果与任务状态传播。
3. 覆盖率与派生数据质量。
4. Scheduler 唯一事实源。
5. 任务互斥、幂等和重启恢复。
6. 数据库职责统一。
7. 数据字段契约统一。
8. 业务消费者逐个迁移。
9. 交易日期和 Freshness 统一。
10. 发布一致性恢复。
11. 产品状态机收口。
12. Outbox、备份、血缘和 E2E 补齐。

实施前禁止：

```text
大规模删除 Legacy 代码
删除旧数据库
重写整个 Warehouse
一次性迁移所有消费者
无备份批量重写生产 Parquet
直接将生产数据状态改为 PASS
```

## 18. 每阶段执行模板

### 现状确认

1. 阅读相关实现和测试。
2. 检查 `git status` 和已有未提交修改。
3. 确认本阶段涉及的生产文件和数据库。
4. 记录修改前专项测试结果。

### 修改约束

1. 只修改本阶段相关文件。
2. 不覆盖用户或其他 Agent 的已有修改。
3. 不删除生产数据。
4. 数据库迁移必须幂等。
5. 除明确修复错误语义外，保持正常 API 行为。

### 验证

1. 运行专项测试。
2. 运行关联回归。
3. 执行 `compileall` 或项目现有等价检查。
4. 必要时运行只读数据诊断。
5. 生产生效后检查容器状态、健康状态、启动日志、关键 API 和 Scheduler 注册状态。

### 部署

纯代码修改通常使用：

```text
sudo docker compose restart stock-web
```

只有依赖或 Dockerfile 变化时才 Build。

### 提交

1. 检查 `git status`、`git diff`、`git log --oneline -10`。
2. 只暂存本阶段相关文件。
3. 测试或验证失败时不得提交半成品。
4. 默认不 Push。

## 19. 总体验收标准

### 数据生成

1. 定时任务真实注册并按 Active Config 执行。
2. 收盘后使用正确交易日作为截止日期。
3. 每条流水线都有明确 Request、Run、Batch 和版本。
4. Quality `FAIL` 绝不进入 Publish。
5. Publish 只发布本次明确版本。
6. 空数据和严重低覆盖数据不能发布。
7. Indicators/Factors 有真实质量门禁。

### 状态流转

1. Request、Run、Batch 和 Dataset 状态一致。
2. 状态转换有合法性约束。
3. Retry 创建新 Run 并保留原上下文。
4. Retry Limit 和部分成功策略真实生效。
5. 重启后无永久 Running。
6. 同一分区不存在并发写入。
7. Scheduler 注册状态与任务中心一致。

### 下游消费

1. 正式业务读取 Published Dataset。
2. Candidate 和 FAIL 数据对业务不可见。
3. 缓存严格按请求日期裁剪。
4. 正式决策不静默在线回退。
5. API 返回数据日期、来源、版本和质量。
6. 模拟和建仓具有服务端数据门禁。
7. 实时失败时明确回退最近收盘价。

### 数据引擎旧库下线验收

1. 生产代码、Scheduler、API、CLI 和测试不再隐式创建或读取 `warehouse/meta.db`。
2. `management.db` 独立承载 instruments、dataset versions/current、quality、source batches 和任务管理事实。
3. `management.db` 与历史 `meta.db` 完成对账，差异均有报告和处理结论。
4. 隔离环境删除 `meta.db` 后，应用、数据访问、指标构建、任务中心和健康检查仍可启动并完成冒烟链路。
5. `meta.db` 备份和归档完成后，删除操作另行取得人工确认，不与代码提交绑定执行。

### 数据事实源

1. 新任务事实只写入 `management.db`。
2. Instruments 与任务 Universe 一致。
3. 数据中心、任务中心和健康 API 使用同一管理库。
4. Legacy DB 只读，不再产生新事实。
5. 数据库升级具备正式迁移版本。

### 运维可靠性

1. 发布中断可自动检测和恢复。
2. Outbox 不会被多个 Worker 重复领取。
3. 备份包含全部关键数据库。
4. 恢复可通过 SQLite 检查和应用启动验证。
5. Compose 启动和核心用户链路具备 E2E 测试。

## 20. 推荐提交序列

```text
fix: validate all task and dataset configs
fix: enforce pipeline quality outcomes
fix: enforce dataset coverage quality
fix: use active task configs for scheduling
fix: make data task execution recoverable
refactor: unify warehouse management metadata
fix: align warehouse dataset schemas
refactor: use published data for portfolio advice
refactor: use published data for market discovery
refactor: use published data for strategy lab
fix: unify trading date and freshness rules
fix: recover interrupted dataset publishing
refactor: enforce product state transitions
fix: improve operational reliability
```

每个提交必须范围聚焦、专项测试通过、不包含生产数据破坏操作，也不得夹带无关工作区修改。
