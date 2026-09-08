# 运行架构拆分与数据任务治理实施方案

> 文档类型：项目内部实施基线
> 
> 用途：后续会话、开发人员和验证人员必须先阅读本文，再实施本主题相关改动。
> 
> 当前状态：方案已确认，阶段 0 尚未开始。本文不是“已经完成”的说明。

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

## 4. 目标架构

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

## 5. Web 与策略验证边界

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

## 6. 分阶段实施

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

## 7. 资源预算原则

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

## 8. 每阶段强制交付格式

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

## 9. 兼容性门禁

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

## 10. 阶段提交约定

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

## 11. 最终验收

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

## 12. 下一步

下一步只能执行阶段 0：只读建立基线。阶段 0 验收前，不得新增 Data Worker、修改 Scheduler、补采 `stock_daily` 或修复历史 `sh68*` 数据。
