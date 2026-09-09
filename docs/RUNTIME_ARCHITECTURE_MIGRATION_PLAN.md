# 运行架构拆分与数据任务治理实施方案

> 文档类型：项目内部实施基线
> 
> 用途：后续会话、开发人员和验证人员必须先阅读本文，再实施本主题相关改动。
> 
> 当前状态：生产现状核对和阶段 0 只读基线已完成；Data Worker/运行架构迁移实施尚未开始。本文不是“已经完成”的说明。

## 1. 执行规则

本文件是当前主题的唯一实施基线。后续会话不得只根据用户临时描述自行扩大或缩小范围；开始工作前必须读取本文、`AGENTS.md`、`docs/INDEX.md`，并核对当前 Git 状态和最近提交。

每次实施只能推进一个阶段。一个阶段必须完成代码、配置、测试、真实场景验证和独立提交后，才允许进入下一个阶段。

若当前工作区存在本主题之外的未提交改动：

- 不得回滚或覆盖这些改动。
- 只暂存本阶段相关文件。
- 在阶段结果中列出未涉及的工作区改动。

若实现与本文冲突：

- 不得静默选择一种解释继续开发。
- 先指出冲突、影响和待确认事项。
- 只有得到明确确认后，才更新本文或调整实施范围。

## 2. 当前实际架构

```text
公网 HTTPS
    |
    v
Caddy
    |
    +-- stock.easyconnect.ltd
    |       |
    |       v
    |   stock-invest 容器
    |       +-- Flask + Waitress Web/API
    |       +-- APScheduler
    |       +-- 页面和 API 请求
    |       +-- 部分 stock_daily 采集、构建、质量、发布
    |       +-- 行业数据/轮动计算
    |       +-- 部分策略分析和批量模拟
    |
    +-- easyconnect.ltd
    |       +-- personal-blog :8000
    |       +-- personal-blog-manage :8001
    |       +-- personal-blog-observatory :8002
    |
    +-- opencode.easyconnect.ltd
            +-- OpenCode Web :4096

stock-invest-business-worker
    +-- 业务异步任务、策略/模拟、通知等
    +-- 当前不是专门的数据生产 Worker
```

当前关键事实：

- `stock-invest` Web 容器与数据任务共用运行空间。
- `stock-invest` 当前配置约为 `1 GiB` 内存上限和 `1.5 GiB` swap 上限。
- `stock-invest-business-worker` 是独立容器，但职责是业务任务，不得直接改造成 stock_daily Worker。
- Scheduler 目前仍在 Web 进程内调度，并会间接执行数据生产任务。
- 数据访问和发布已经使用 `DatasetAccess`、Raw Batch、Candidate、Quality、Published Dataset 和 `dataset_current`。
- 现有任务/数据表包括 `task_execution_requests`、`job_runs`、`task_run_events`、`source_batches`、`dataset_versions`、`dataset_current` 等。

## 3. 已确认故障

### 3.1 Web 容器 OOM

历史内核日志出现：

```text
Memory cgroup out of memory
Killed process ... waitress-serve
```

这说明数据任务或重型页面请求的内存峰值会杀掉 Web 服务，而不是只杀掉单个数据任务。

### 3.2 全市场采集超过单次 deadline

当前约 6,915 个标的、腾讯请求间隔约 0.3 秒，仅间隔时间就约为：

```text
6915 × 0.3 秒 ≈ 2074 秒
```

这还不包含 HTTP、解析、写 Raw、状态更新和失败重试时间。当前 `1800` 秒全任务 deadline 不能保证成功。

### 3.3 输入日期错位导致轮动字段为空

当 `industry_daily` 已到 `T` 而 `stock_daily` 只有 `T-1` 时，轮动计算不能使用行业单边数据。否则市场基准和相对强度可能为 `NaN`，阶段会全部落到默认状态。

### 3.4 Tencent 历史单位遗留

当前新代码已记录 Tencent Raw 单位并处理 `sh68*` 特殊成交量口径，但历史 Published 分区仍存在大量单位异常。历史修复必须单独版本化，不能与运行架构拆分混做。

## 4. 生产收口现状核对（2026-09-08）

本节是对当前代码和生产管理库的事实记录，不是目标状态声明。

### 4.1 已落地

- `Warehouse` 的生产默认管理库已指向 `management.db`。
- `DatasetAccess` 已作为正式数据读取入口，读取 `dataset_current` 并校验 Published 状态、质量、文件和 checksum。
- 行业成员、行业日线、行业特征和行业轮动已有 Published 链路。
- 已在生产 `management.db` 中发现相关任务定义和部分 enabled 配置；具体 active/enabled/run/output/quality/current 对账尚未完成。
- `instruments` 当前由 `management.db.instruments` 提供基础标的目录服务，Web 不直接写 SQL。
- 公开 API、静态 JSON 和 `/market` 已有独立访问入口。

### 4.2 尚未闭环

1. `web/app.py` 的股票分类接口仍存在 fundamentals 缺失后在线采集并写仓库的路径，尚未满足 Published-only。
2. `web/scheduler.py` 仍在 Web 进程内触发行业采集、行业特征、行业轮动和其他数据任务，独立 Data Worker 尚未建立。
3. `allow_legacy` 仍存在于生产可调用模块；默认值多数为关闭，但尚未完成生产入口分类审计和能力收口。
4. `instruments` 是管理库基础目录服务还是未来 Published Dataset，服务边界尚未最终定稿。
5. 生产验收证据不足：尚未完成全量任务闭环证明、Published 文件对账、隔离旧库冷启动、生产旧库读写证明、只读观察期和旧任务台账对账。

### 4.3 验收证据要求

任务定义存在不等于生产闭环完成。每个生产任务都必须分别核验：

```text
active definition
+ enabled
+ recorded run
+ output version
+ quality result
+ dataset_current
```

生产切换还必须证明：

- `dataset_current`、`dataset_versions`、Published 文件和 checksum 一致。
- 隔离旧库后 Web、任务中心、调度注册、数据访问和健康检查可以冷启动。
- 生产运行期间旧库没有读取和写入。
- 旧任务台账与 `management.db` 已完成对账并进入只读观察。
- Web 原始入口到最终页面结果的完整链路验证通过。

### 4.4 旧库引用分类原则

全局出现旧库名称或 `allow_legacy` 不等于生产依赖。引用必须归类为：

| 分类 | 处理原则 |
|---|---|
| 生产运行时读取/写入 | 必须迁移、阻断或删除 |
| 测试 fixture | 可保留，但必须使用隔离临时库 |
| 一次性迁移工具 | 可保留显式历史输入，不得被运行时调用 |
| 备份工具 | 可保留归档输入，不得作为业务读取源 |
| 诊断工具 | 只读并标明历史/归档语义 |
| 文档 | 区分当前事实、目标状态和历史记录 |

不能因为 `Warehouse()` 默认使用 `management.db`，就宣布全局生产收口完成。

### 4.5 阶段 0 基线产物

阶段 0 已完成只读核验，报告如下：

- [生产管理库任务审计](audits/production_management_db_task_audit_20260908.md)
- [生产 Dataset Current 对账](audits/production_dataset_current_reconciliation_20260908.md)
- [生产旧库运行时引用审计](audits/production_legacy_runtime_reference_audit_20260908.md)

阶段 0 只证明当前事实和缺口，不代表 Data Worker 已建立、Scheduler 已停止执行数据生产，或旧库已经完成零读写验收。

## 5. 目标架构

不更换 Flask、Waitress、APScheduler、Docker Compose、SQLite 和现有数据治理链路。不引入 Celery、Kafka、Redis Queue。

```text
                         Caddy
                           |
                           v
                 stock-invest-web
                 Flask + Waitress
                 页面、轻量 API、任务提交、结果展示
                           |
             ┌─────────────┴─────────────┐
             │                           │
             v                           v
        management.db              Published Dataset
             ▲                           ▲
             │                           │
 stock-invest-scheduler       stock-invest-data-worker
 APScheduler                  单进程数据生产 Worker
 只判断、建 Request、触发         Capture / Build / Quality / Publish
 不执行 Pandas 数据生产          指标、行业特征、轮动、数据修复

                 stock-invest-business-worker
                 策略、回测、模拟、业务通知
```

职责边界：

| 角色 | 允许职责 | 禁止职责 |
|---|---|---|
| Web | 页面、轻量查询、单股有限历史、任务提交、结果展示 | 全市场采集、全市场指标生产、大批量回测、直接生产 Published 数据 |
| Scheduler | 判断交易日/缺口、创建 Request、触发和监控 Worker | 直接调用 Collector、Builder、Quality、Publisher 或大规模 Pandas |
| Data Worker | stock_daily、行业数据、指标构建、质量、发布、数据修复 | 提供 Web、启动 APScheduler、执行业务策略任务 |
| Business Worker | 单股/小批量策略、回测、模拟、通知 | 写入 stock_daily 正式数据 |

## 6. Web 与策略验证边界

本文不禁止 Web 使用历史数据。真正的边界是数据规模和任务重量：

### Web 保留

- 单只股票有限日期范围查询。
- 单只股票研究。
- 单只股票单策略轻量回测。
- 已发布指标和行情的窄范围读取。
- 当前公开 API 和静态 JSON。

### Business Worker 执行

- 多股票批量模拟。
- 多策略比较。
- 参数网格搜索。
- 长区间批量回测。
- 批量生成收益曲线。
- 大规模研究任务。

### Data Worker 执行

- 全市场行情采集。
- Raw -> Build -> Quality -> Publish。
- 全市场指标重建。
- 全市场扫描需要的数据生产。
- 行业特征和行业轮动生产。
- 历史单位修复。

## 7. 分阶段实施

### 阶段 0：基线

目标：只读记录当前节点、容器、任务链、数据版本、测试和 OOM 事实。

禁止：改代码、改配置、重启/停止服务、补采数据、修复历史数据。

必须输出：

- 容器清单、启动命令、内存限制和实时占用。
- 宿主机 RAM、Swap、磁盘和 OOM 记录。
- Web、Scheduler、Business Worker、Data Worker 当前职责。
- Active Config 与任务链。
- `stock_daily`、`industry_daily`、`industry_rotation_daily` 最新日期。
- 最近失败/超时 JobRun、Source Batch 和下游状态。
- 公开 API、静态 JSON、`/market` 状态。
- 相关测试基线，包括已有失败。

验收：基线结果可回答“谁在执行什么、数据到哪天、最近为何失败”，并保存为内部报告或阶段记录。

### 阶段 1：新增 Data Worker

目标：增加独立 `stock-invest-data-worker` 运行角色，先不切断旧生产路径。

范围：

- 新增单进程 Data Worker 入口。
- 不启动 Flask、Waitress、APScheduler。
- 读取现有 `management.db`。
- 能记录 Worker 心跳和任务事件。
- 初期可以只接入显式测试任务或 `stock_daily_capture` 的受控任务。
- Docker Compose 增加 Worker 服务，复用镜像、代码挂载、output 和管理库。

初始资源只作为待验证建议，不得未经阶段验收直接调大：

```text
mem_limit: 600m-700m
pids_limit: 64
```

验收：容器可启动、无 Waitress/APScheduler、只有一个 Worker 主进程、能连接正确管理库、Web/API/Business Worker 不受影响。

#### 阶段 1 当前实施记录（2026-09-08）

- 已新增 `ops/data_worker.py`，作为不启动 Flask、Waitress 或 APScheduler 的独立数据 Worker 入口。
- Worker 使用 `task_execution_requests`，按 allowlist 原子领取请求，并复用 `ops.task_execution.execute_task` 执行已注册数据任务。
- Worker 同时校验任务定义的 `task_type`，只允许数据平面类型，避免通过环境变量误领取业务任务。
- Worker 写入 `data_worker_heartbeats`，记录进程、主机、状态和当前 Request；异常时将已领取 Request 收口为 `failed`，任务返回的 `timeout` 等终态保持原状态。
- `docker-compose.yml` 已增加 `data-worker` 服务，但置于 `data-worker` profile 下，阶段 1 不自动改变现有生产执行者；服务使用独立容器、管理库和 `700m` 内存上限建议值。
- 已通过临时管理库冒烟验证：原子领取和重复领取阻断、异常失败收口、超时结果和心跳恢复为 idle、Compose profile 配置解析。
- 当前未完成：Scheduler 切换、旧生产路径切断、全市场任务接管、真实容器启动验证和完整 pytest 回归。

### 阶段 2：Scheduler 只调度

目标：APScheduler 不再直接执行数据生产。

范围：

- Scheduler 只判断交易日、缺口和前置条件。
- 只创建 `task_execution_requests` 并触发 Data Worker。
- Data Worker 执行 Capture、Build、Quality、Publish。
- 上游失败/超时/部分完成时，下游必须阻断。
- 行业和股票实际数据日期不一致时，轮动必须是 `blocked_by_upstream`，不能发布。
- 切换期间可保留旧函数，但生产运行路径不得调用；切换完成后收口，不能长期双写。

验收场景：上游成功完整链路；Capture 失败只产生 Capture 失败；Capture 超时不执行下游；输入日期不一致时轮动阻断；同一任务/日期只有一个有效执行者。

#### 阶段 2 当前实施记录（2026-09-08）

- `web/scheduler.py` 的日线链路、辅助数据链路和封盘重试链路已改为只创建数据 Request，不再在这些生产调度路径中直接调用 Collector、Builder、Quality 或 Publisher。
- Scheduler 入队按任务和周期幂等去重，并保留 `task_timeout`、`as_of`、输入版本等结构化 Request payload。
- `ops.data_worker.DataWorker` 在数据阶段成功后按固定日线链路创建下游 Request；上游失败、timeout 或非 success 时不推进下游。
- `task_execution_requests` 增加 `request_payload` 字段，并对已有管理库执行非破坏性列补齐，保证 Scheduler 到 Data Worker 的参数不丢失。
- Data Worker 默认 allowlist 已覆盖配置的数据任务，且仍通过 `task_type` 校验隔离业务任务。
- 阶段 2 相关测试共 28 项通过，包含入队幂等、Scheduler 无直接生产调用、上下游成功/失败阻断、任务配置和日期对齐测试。
- 当前未完成：Data Worker 尚未接管生产服务；Compose 仍使用显式 profile；全市场批次化和断点续跑留在阶段 3。

#### 阶段 2 生产切换记录（2026-09-08）

- 已通过 `docker compose --profile data-worker up -d data-worker` 启动 `stock-invest-data-worker`，容器状态为 `running/healthy`。
- Data Worker 已写入生产 `management.db` 心跳，当前状态为 `idle`，没有遗留 Request 被自动执行。
- Data Worker 启动时已对已有 `task_execution_requests` 做非破坏性 schema 补齐，`request_payload` 字段已存在。
- 已重启 `stock-web` 使新的 Scheduler 代码加载；Web、Data Worker、Business Worker 均保持 healthy。
- 切换后生产队列为空，因此本次只验证了进程和调度角色切换，尚未执行切换后的真实采集、构建、质量或发布任务。
- 切换后的第一条真实数据任务必须使用明确日期和受控证券范围验证；不得直接用全市场采集替代阶段 3 的批次化验收。

#### 阶段 2 受控验证记录（2026-09-08）

- 原计划验证单证券 `sh600000`、明确日期 `2026-09-07`，但 Scheduler 在检查窗口内自动创建了空 `symbols` 的全市场 `stock_daily_capture` Request，范围为 6915 个标的。
- 经用户确认后已停止 `stock-invest-data-worker`，没有删除已产生的 4 个 Raw Parquet 文件（约 68 MB）。
- Request `req_20260908220001_8c79c95ddf`、JobRun `2608` 和 SourceBatch `tencent_20260908220045_3d16bf0fee` 均已收口为 `failed`，失败原因记录为用户确认的人工中止，并已释放任务锁。
- 本次没有生成新的 `stock_daily`/`indicators` Dataset Version，也没有更新 Published Current；Web 和 Business Worker 保持 healthy。
- 通知失败事件尝试写入通知库时遇到只读数据库错误，但不影响上述任务事实收口；该问题另行处理。
- 本次暴露出阶段 3 前的运行风险：Scheduler 的自动触发可能在受控验证窗口内产生全市场 Request，后续必须增加受控范围开关或隔离验证环境。

#### 数据接口收口记录（2026-09-08）

- 停机隔离期间，正式默认数据源已收口为 Published-only `WarehouseSource`；无 Published 版本时不再隐式读取旧 daily 分区。
- `AnalysisEngine` 不再在 K 线、基本面和分红读取失败时直接访问在线源；基本面统一通过 Published `fundamentals`，分红在尚无 Published 契约时明确标记 `unavailable`。
- `web/app.py` 的 `/api/classify` 缺少 Published fundamentals 时返回 `503 unavailable`，不再触发在线采集或写回仓库。
- `portfolio/dashboard.py` 的基本面快照已改为通过 Published `fundamentals` 读取；`biz/triggers.py` 的指标判断已改用正式默认 Published 数据源。
- `FallbackDataSource` 和 `OnlineSource` 仍保留为显式测试/隔离研究能力，正式 Web、Core、Portfolio、Biz 入口不再直接构造或使用它们。
- 数据源相关回归共 31 项通过；扩大到持仓、通知和数据任务相关回归时，仍有既有通知规则数量断言和 `watch_pool` fixture 字段缺失两项失败。
- `portfolio/monitor.py` 的正式股息锚路径已停止直接访问在线分红接口；在分红 Published 契约建立前明确返回不可用。
- 当前 Web 和 Business Worker 已恢复；Data Worker 保持停止，待增加受控范围保护后再启动并执行真实数据链路验证。

#### 数据接口迁移生产验证记录（2026-09-08）

- Web、Business Worker 停机隔离期间完成正式入口收口：`WarehouseSource` 默认只读 Published `stock_daily`，Core/Portfolio/Biz/Web 不再在正式路径直接在线采集或隐式 legacy 回退。
- `DatasetAccess` 对 symbol 分区数据集已按请求 symbol 精确选择版本，避免 `/api/classify` 为读取一个 fundamentals 文件扫描全部历史文件。
- 重启 Web 后在容器内验证 `fundamentals/sh600000` Published 读取 20 行、耗时约 `0.061s`；原始入口 `/api/classify?code=600000` 返回 HTTP 200，结果包含行业、ROE 和营收增速。
- 同时验证了无 Published 日线的正式 `WarehouseSource` 会抛出 `DatasetAccessError`，不会读取旧 daily 分区。
- 本次未启动 Data Worker 执行真实数据 Request，未新增数据版本，未更新 Current；Web 和 Business Worker 已恢复 healthy。
- 仍有两个非本次改动相关的既有测试失败：通知默认规则数量断言（当前用户配置为 13 条，测试期望 12 条）和 `watch_pool` 测试 fixture 缺少 `asset_type` 字段。

### 阶段 3：stock_daily 批次化与断点续跑

目标：一次 OOM、超时或 Worker 重启只影响当前批次。

初期复用现有 Request、JobRun、Source Batch、failure details 和 checkpoint，不立即新增海量股票级任务表。

范围：

- 全市场切成约 50-100 只一批，具体大小由内存实测决定。
- 每批串行请求、立即写 Raw Batch、更新状态、释放内存。
- 成功且已有目标日期数据的股票不重复请求。
- 失败和未处理股票进入下一批。
- 区分单 HTTP timeout、批次 timeout、Run deadline 和 heartbeat timeout。

验收：受控中断后重启只处理剩余股票；成功股票不重复请求；每批有统计；最终完整成功可进入 Build；失败股票不被误记成功。

#### 阶段 3 当前实施记录（2026-09-08）

- `ops.data_worker.DataWorker` 已增加通用批次执行入口：只对 `stock_daily_capture` 进行证券批次化，其他任务仍按独立数据任务执行。
- 普通证券日线按管理库 `instruments.type` 和 `asset_profiles` 分为 `stock`、`etf` 两组，分别切成默认 50 只一批；指数和行业不会混入普通证券日线批次。
- 每个子批次 Request 保存 `asset_types`、`batch_index`、`batch_count`、`parent_request_id` 和原始执行 payload；父 Request 汇总子批次结果和 Source Batch ID。
- 股票和 ETF 共用 `stock_daily_capture` 的采集适配和限流机制，但后续质量/指标适用性仍以各自 profile 的 `required/optional/not_applicable` 为准。
- 已复用现有 Raw Batch 的成功项跳过和 partial Raw 恢复逻辑；没有新增股票级海量任务表。
- 修复 Request ID 同秒冲突：`TaskCenter.create_request()` 改用 UUID 后缀，保证同一父任务下多个类型批次可并行持久化而不发生主键冲突。
- 阶段 3 测试共 29 项通过，另有已有 SourceBatch/财务报告 checkpoint 测试 8 项通过。
- 当前未完成：真实生产批次接管、受控中断后的跨进程恢复演练、质量按类型统计接入和指数/行业独立链路的进一步批次化。

#### 阶段 3 受控生产验证记录（2026-09-08）

- Web 保持停止，仅在生产 `management.db` 创建了明确日期 `2026-09-07`、证券范围 `sh600000`（stock）和 `sh510300`（ETF）的受控 Request，批次大小为 1。
- Data Worker 成功领取父 Request，并按类型创建子批次；先执行 `sh600000`，未扩大到全市场或 ETF 批次。
- `sh600000` 子批次运行超过设定的 120 秒仍未完成，Data Worker 心跳与 JobRun 状态出现短暂不一致；经确认后停止 Data Worker。
- 父 Request `req_controlled_20260908233047_e900de79` 已为 `failed`；子 Request `req_20260908233104_710c2ad8896c` 已为 `timeout`；JobRun `2609` 已为 `timeout`；SourceBatch `tencent_20260908233104_7306c25f2e` 已为 `failed`。
- 没有生成新的 `stock_daily`/`indicators` Dataset Version，也没有更新 Published Current；已有 Raw 文件保留，任务锁已释放，待处理 Request 为 0。
- 验证结论：类型分组和子批次创建有效，但 whole-task deadline、Worker 心跳与实际执行状态一致性、跨进程中断恢复仍未达到验收要求；不能宣称阶段 3 生产批次接管完成。
- 通知失败事件再次遇到通知库只读错误；任务事实本身已正常收口，通知库问题需单独修复。

#### 阶段 3 超时收口修复记录（2026-09-08）

- Data Worker 现在将每个有 `task_timeout` 的数据阶段放入可终止子进程执行，父 Worker 以 whole-task deadline 为准等待并在超时后终止子进程。
- 超时收口会统一处理 Request、JobRun、SourceBatch 和任务锁；不存在 SourceBatch 表的隔离任务也不会覆盖原始超时错误。
- 子进程返回空结果会被视为失败，不再触发 `None.get` 类二次错误。
- 新增测试覆盖阻塞子进程、子进程已写入 running JobRun/SourceBatch 后超时、锁释放和无 running 残留；数据任务阶段相关测试共 58 项通过。
- 当前仍有 multiprocessing 在多线程进程中使用 `fork` 的 Python 3.12 DeprecationWarning；不影响功能测试，但后续可评估改用更安全的启动上下文。

#### 阶段 3 修复验证记录（2026-09-08）

- 已新增阻塞子进程的隔离测试：子进程先写入 running JobRun 和 SourceBatch，再超过 deadline；父 Worker 能终止子进程并将相关事实收口，且无遗留 running 锁。
- 修复了无 deadline 子任务异常、子进程空结果和临时库缺少 SourceBatch 表时的错误覆盖问题。
- 阶段 3 相关回归共 58 项通过；当前仍未重新执行生产真实采集，生产批次接管仍需后续受控验证。

#### 覆盖索引改造记录（2026-09-09）

- 新增 `warehouse/coverage.py`，在 `management.db` 中维护 `dataset_entity_coverage` 快速覆盖表和 `dataset_entity_date_status` 日期级状态表。
- `MarketCollector.sync_daily()` 日常增量判断改为按当前选中实体和 `asset_type` 查询 coverage，不再扫描历史 Raw 文件做全市场 `GROUP BY code`。
- 成功、失败、空响应和 timeout 会分别更新日期级状态、最后成功日期、最后尝试日期、失败次数和 Source Batch 关联。
- 增加显式 `scripts/rebuild_entity_coverage.py`，仅供一次性、操作员指定文件的历史索引重建，Scheduler/Data Worker 不会自动调用。
- 股票和 ETF 共用 `stock_daily`，但 coverage 按 `entity_type` 分开；指数和行业可以复用覆盖服务，但仍保持独立数据集和任务链。
- 覆盖索引和类型隔离相关测试共 34 项通过；尚未执行生产 coverage 全量重建，也未重新启动生产 Data Worker。

#### Universe 双路径记录（2026-09-09）

- 新增 `warehouse/universe.py`，维护 `universe_snapshots` 和 `universe_snapshot_items`，记录清单来源、是否权威、是否完整、交易状态和 active 标记。
- 权威全量清单成功时由 `MarketCollector.sync_instruments()` 写入 `instruments` 和当日快照；空清单或源异常时优先使用最近历史快照，再退回现有目录。
- 非权威历史/目录兜底只用于继续识别采集对象，不会把未出现在兜底清单中的证券标记为退市；只有后续明确的权威清单流程才允许执行下架判定。
- Data Worker 的空 `symbols` Request 优先读取目标日期之前最近的 Universe 快照，并按任务 scope 过滤；找不到快照时才使用 `instruments.universe_status='active'` 目录。
- 新增交易状态、Universe 生命周期字段到 `instruments`：`trade_status`、`universe_status`、`first_seen_date`、`last_seen_date`、`delisted_date`、`last_source`。
- Universe 双路径、交易状态和历史兜底测试已通过 30 项；尚未在生产执行当天全量 Universe 同步。

#### Universe 生命周期收口记录（2026-09-09）

- 权威全量快照成功时，`UniverseStore.reconcile_authoritative_snapshot()` 会把目录中未出现在本次清单的 active 实体标记为 `inactive_candidate`，不直接标记 `delisted`，也不删除历史数据。
- 权威清单为空或获取异常时，只能使用历史快照或 active 目录兜底；该路径不会改变既有证券的 active 状态，避免源故障造成误下架。
- `stock_daily_capture` 的空范围 Request 已接入 Universe 前置解析：权威清单优先，失败时历史快照/目录兜底，并将来源、权威性和快照日期传入 SourceBatch 上下文。
- 新增/更新 Universe 生命周期和 Data Worker 前置解析测试，相关测试共 40 项通过；尚未执行生产全量 Universe 同步和退市确认策略。

### 阶段 4：Capture/Build/Quality/Publish 收口

目标：数据生产阶段独立落盘和传递版本。

目标链路：

```text
Capture -> Raw Batch -> RAW_READY
全部 Capture 完成 -> Candidate
Candidate -> Quality
Quality PASS/WARNING -> Publish
Publish -> dataset_current 切换
```

禁止：Web 生产数据；隐式寻找历史 Candidate；部分 Raw 当完整输入；质量失败仍发布；旧数据伪装当天数据。

验收：数据库可还原 Request -> JobRun -> SourceBatch -> Candidate -> Quality -> Published；任意阶段失败时后续阶段不执行；重启无永久 running 和残留锁。

### 阶段 5：策略任务边界

目标：单股验证体验不变，批量/重型策略移至 Business Worker。

验收必须覆盖原始完整路径：

```text
原页面 -> 用户操作 -> 创建任务 -> Worker 执行 -> 结果落盘 -> 结果页面展示
```

单股轻量策略仍可用；批量策略返回 `202 + run_id` 并由 Business Worker 执行；Web 不执行大规模回测。

### 阶段 6：心跳、OOM 和失败治理

目标：区分 `CGROUP_OOM`、`WORKER_LOST`、`BATCH_TIMEOUT`、`SOURCE_ERROR`、`DATA_QUALITY_FAIL`、`PUBLISH_FAIL` 和 `BLOCKED_BY_UPSTREAM`。

建议 Data Worker 主进程管理重型子进程，记录退出码、最后批次、最后股票、RSS 和心跳。`exit code 137` 或 cgroup OOM 事件应标记 `CGROUP_OOM`。

验收：模拟子进程终止后 Run 不永久 running，页面显示失败原因，下一次只恢复未完成批次，Web/API 仍可访问。

### 阶段 7：历史 sh68 单位修复

目标：在运行架构稳定后修复历史成交量单位，不与前面阶段混做。

必须生成 V2 Candidate、质量结果、差异报告和回滚依据后再切换 `dataset_current`。

验收至少包括：记录数不变、OHLC 不变、非 `sh68*` 不变、修正数量可解释、异常比例下降、指定股票抽样正确、旧版本可回滚。

## 8. 资源预算原则

当前节点约 2 GiB RAM，另有 OpenCode、博客、Caddy、Redis 和系统服务。预算不是承诺值，必须通过阶段实测调整。

初始目标：

| 服务 | 正常目标 | 初始硬限制建议 |
|---|---:|---:|
| Web | 200-300 MiB | 400-450 MiB |
| Scheduler | 30-80 MiB | 128 MiB |
| Data Worker | 300-450 MiB | 600-700 MiB |
| Business Worker | 当前较低 | 256-384 MiB |
| OpenCode | 当前约 289 MiB | 单独评估 |

不得通过提高并发解决问题。Tencent 请求保持串行和现有限速。Swap 只作为保险，不作为持续超额运行方案。

## 9. 每阶段强制交付格式

```text
阶段名称：
目标：
本阶段范围：
明确未做事项：
改动文件：
运行角色变化：
数据库变化：
容器变化：
任务链变化：
测试命令：
测试结果：
真实场景：
真实场景输入：
预期结果：
实际结果：
兼容性检查：
未解决问题：
提交：
是否允许进入下一阶段：
```

没有真实场景结果，不能声称阶段完成。没有独立提交，不能进入下一阶段。

## 10. 兼容性门禁

每个阶段都必须检查：

- 任务 key、Active Config 和现有任务表仍可读取。
- 同一任务/交易日不会被两个角色同时执行。
- 失败、超时、部分完成不会被标记成功。
- 上游失败不会执行下游。
- Raw Batch、Candidate、Quality、Published 数据不被删除。
- `dataset_current` 不指向不可读或质量不允许的版本。
- 交易日和日期边界明确。
- 单位元数据不丢失。
- `/market` 可打开。
- 单股研究和单股策略验证可用。
- `/public-api/health`、`/public-api/stock/daily` 返回 JSON。
- `/public-data/*.json` 仍由 Caddy 直出。
- Business Worker 不生产 `stock_daily`。
- Data Worker 不提供 Web，也不启动 Scheduler。
- OpenCode、博客和其他无关服务不被停止或改动。

## 11. 阶段提交约定

每阶段独立提交，提交前必须检查 `git status`、`git diff`、`git log --oneline -10`，只暂存本阶段文件，不提交 `.env`、密钥或无关改动。

建议提交风格：

```text
docs(runtime): establish architecture migration baseline
feat(runtime): add data worker role
refactor(scheduler): enqueue data requests only
feat(data): resume capture by batches
refactor(data): close staged production pipeline
refactor(web): route heavy jobs to workers
feat(runtime): classify worker failures
fix(data): publish corrected stock daily versions
```

## 12. 最终验收

最终必须证明：

1. Web OOM 不会杀 Data Worker。
2. Data Worker OOM 不会杀 Web。
3. Scheduler 不执行 Pandas 数据生产。
4. `stock_daily` 只由 Data Worker 生产。
5. Business Worker 不写 `stock_daily` 正式数据。
6. 全市场采集按批次执行。
7. Worker 重启后只续跑未完成批次。
8. 已成功股票不重复请求。
9. 上游失败会阻断下游。
10. 行业与股票日期不一致时不会生成轮动结果。
11. 单股策略验证仍可用。
12. 批量策略验证仍可用且异步执行。
13. OOM、超时、源错误和质量失败可区分。
14. Published 数据可回滚。
15. 历史单位修复不直接覆盖旧版本。
16. `/market`、公开 API、静态 JSON 均可访问。
17. 节点内存、Swap 和磁盘保持安全。

## 13. 下一步

下一步只能执行阶段 0：只读建立基线。阶段 0 验收前，不得新增 Data Worker、修改 Scheduler、补采 `stock_daily` 或修复历史 `sh68*` 数据。
