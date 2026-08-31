# 平台支撑与运行治理子模块设计 V1

## 1. 模块定位

本模块负责让业务模块可靠运行、可观察、可恢复和可操作，包括：

```text
任务中心
调度与 Worker
数据中心和健康
配置生效
权限认证
备份恢复
API/UI 平台边界
```

它不定义选股、策略、交易和收益的业务规则，只提供统一运行能力。

本文定义新业务平台运行模型。数据平面的现有任务载体和 `management.db` 继续负责 capture/build/quality/publish/indicators 等数据任务；本平台只负责业务任务、业务运行库和业务调度，不替换数据平面。

## 2. 第一版目标

1. Web、Scheduler 和任务执行使用清晰的边界。
2. 任务状态持久化并可追踪。
3. Scheduler 以 Active Config 为事实源。
4. 手工、定时和重试任务使用同一 Runner。
5. 任务具备互斥、幂等、超时和重启恢复。
6. 健康检查能区分存活、就绪和依赖异常。
7. 高风险操作具备授权、确认和审计。
8. 备份覆盖所有关键数据库和配置。
9. 关键 API 返回统一错误结构。

## 3. 任务运行模型

```text
BusinessTaskDefinition
→ ActiveConfig
→ BusinessExecutionRequest
→ BusinessJobRun
→ Worker
→ Artifact / Event
→ Terminal Result
```

### 3.1 状态

```text
requested
→ running
→ success
→ partial_success
→ failed
→ cancelled
```

### 3.2 运行记录

每次运行必须保存：

```text
request_id
run_id
task_key
config_version
trigger_type
input_versions
output_versions
attempt
started_at
heartbeat_at
finished_at
error_code
error_message
```

### 3.3 互斥与幂等

任务锁键至少包含：

```text
task_key
partition / period
dataset write group
```

锁采用数据库租约，支持：

```text
owner_run_id
acquired_at
heartbeat_at
expires_at
```

## 4. Scheduler 设计

### 4.1 事实源

```text
YAML → 配置发现和草稿
management.db active config → 运行事实
APScheduler → 运行时投影
```

启用、停用和激活后必须重新加载数据任务，且 API 返回实际注册结果。

### 4.2 顶层流水线

建议定时只注册顶层业务流水线：

```text
stock_daily_pipeline
→ capture
→ build
→ quality
→ publish
→ indicators
→ factors
```

阶段任务可保留人工执行入口，但不能依赖多个独立定时任务按时间碰撞完成链路。

### 4.3 Web 与 Worker

第一版可以继续使用单容器，但必须做到：

1. HTTP 请求只创建并持久化 Request/Run。
2. 长任务进入持久化执行队列。
3. Worker 不依赖 daemon thread 状态。
4. Web 重启后可以恢复、回收或重新接管任务。

Worker 可以先与 Web 同进程运行，但必须遵守新业务任务接口；不得复用旧业务任务状态和旧业务运行入口。数据任务继续使用数据平面自己的执行入口。

业务任务接入统一提供：

```text
business_task_key
input_schema
config_version
run(input)
result_schema
artifacts
```

接入任务包括：

```text
screen.run
research.run
simulation.run
parameter_search.run
report.daily_generate
observation.expiry_reconcile
advice.refresh
notification.outbox_delivery
health.reconcile
```

长任务必须创建 BusinessRequest/BusinessJobRun；轻量维护任务也必须写业务运行记录；真实交易不进入后台任务框架。

## 5. 重启恢复

应用启动时执行：

```text
回收 stale ExecutionRequest
回收 stale JobRun
回收 stale SourceBatch
释放过期任务锁
检查 publishing version
检查临时文件
```

处理规则：

1. 旧 `running` 标记为 `failed`，错误码为 `PROCESS_RESTARTED`。
2. 有明确 checkpoint 的任务可以重新接管，没有 checkpoint 的任务重新执行。
3. 不删除正式文件。
4. 不把未知状态直接标记为成功。
5. 恢复过程写入事件和操作日志。

## 6. 数据中心和健康检查

### 6.1 三类健康探针

```text
/health/live   # 进程是否存活
/health/ready  # 关键依赖是否可用
/health/details # 管理页面详情
```

### 6.2 就绪依赖

至少检查：

- management.db 可读写；
- portfolio.db 可读写；
- Published Dataset 元数据一致；
- Scheduler 状态；
- Worker/任务队列状态；
- 磁盘空间；
- 内存阈值；
- SMTP 配置仅在通知功能启用时检查。

### 6.3 健康结果

统一结构：

```json
{
  "status": "degraded",
  "severity": "warning",
  "code": "DATA_STALE",
  "component": "stock_daily",
  "blocking": false,
  "observed_at": "...",
  "details": {}
}
```

区分：

```text
disabled
not_run
running
failed
stale
quality_failed
healthy
```

## 7. 配置管理

配置按生命周期管理：

```text
draft
→ validated
→ active
→ superseded / disabled
```

配置保存必须记录：

```text
operator
changed_at
before_hash
after_hash
validation_result
activation_result
```

高风险配置包括：

- 任务调度；
- 数据发布；
- 策略启用；
- 通知收件人；
- 数据重建；
- 数据重置。

这些操作必须显示影响范围并要求二次确认。

## 8. 权限与安全

第一版虽然可以保持单用户，但仍必须：

1. 生产默认启用认证。
2. 禁止 Compose 固定关闭认证。
3. Secret Key 必须来自环境变量，禁止固定开发 Secret 进入生产。
4. 高风险写操作需要 CSRF Token 或等价一次性确认机制。
5. 邮件、Webhook、SMTP 密码不出现在日志和响应中。
6. 测试通知限制目标地址和域名，防止 SSRF。
7. 错误响应不得包含密钥、绝对内部路径和敏感配置。

后续再增加 RBAC，不影响第一版单用户模型。

## 9. 备份与恢复

### 9.1 必备份内容

```text
management.db
portfolio.db
notification_outbox.db
warehouse/
config/
schemes/custom/
notifier configuration
task logs and runtime metadata
```

### 9.2 备份要求

1. 使用 SQLite backup API。
2. 备份目录不得位于生产数据目录内部。
3. 生成备份 manifest 和 checksum。
4. `.env` 和密钥备份必须加密并限制权限。
5. 备份成功后执行 SQLite `quick_check`。
6. 保留策略和离机复制由运维配置决定。

### 9.3 恢复演练

```text
恢复到临时目录
→ quick_check
→ 校验文件 checksum
→ 启动临时应用
→ 查询任务、版本、持仓和 Outbox
→ 验证关键 API
```

恢复不得直接覆盖生产目录，除非经过明确人工确认和备份前置检查。

## 10. API 平台规范

统一成功响应：

```json
{
  "data": {},
  "request_id": "..."
}
```

统一错误响应：

```json
{
  "error": {
    "code": "TASK_CONFLICT",
    "message": "任务正在运行",
    "retryable": true,
    "details": {}
  },
  "request_id": "..."
}
```

任务接口应区分：

```text
参数错误
资源不存在
配置未激活
任务冲突
依赖未满足
已提交
正在运行
部分成功
执行失败
```

## 11. UI 平台边界

`web/app.py` 可以暂时保留路由入口，但领域逻辑必须下沉到服务层：

```text
Route
→ Application Service
→ Domain Service
→ Repository
```

页面不得：

- 直接修改状态字段；
- 直接调用底层数据采集器；
- 直接拼接收益口径；
- 自己决定是否允许高风险操作；
- 用前端按钮代替服务端门禁。

迁移期间可以保留下线提示，但最终导航和 API 只保留新业务系统正式入口；数据平面入口由数据模块继续负责，旧业务入口在切换完成后删除。

## 12. 当前能力映射

| 当前能力 | 目标归属 |
|---|---|
| 新 `BusinessTaskCenter` | Business Task Definition / Request service |
| 新 `BusinessTaskRunner` | Business Worker runtime |
| 新 `BusinessJobRunRepository` | Business Run repository |
| 新 `BusinessScheduler` | Business Scheduler runtime |
| 数据模块 `management.db` | Dataset fact store，仅由数据平面维护 |
| 新 `HealthService` | Health/Readiness |
| `runtime/memory.py` | Resource probe |
| 新 `BackupService` | Backup and restore |
| `web/app.py` | Route and composition root |

## 13. 实施步骤

1. 修复 Active Config 与 Scheduler 注册不一致。
2. 统一 Request/Run/Worker 返回结果。
3. 增加任务锁、幂等键和 heartbeat。
4. 接入启动回收和发布恢复检查。
5. 拆分 live/ready/details 健康接口。
6. 修复认证、CSRF、高风险操作确认和密钥管理。
7. 扩充备份范围并增加恢复验证。
8. 统一 API 错误结构。
9. 将领域逻辑逐步从 `web/app.py` 下沉。

## 14. 测试与验收

1. Active Config 改变后 Scheduler 真实更新。
2. 同一任务和分区不能并发运行。
3. HTTP 返回 202 前 Run 已持久化。
4. 重启后 stale run 被正确回收。
5. 任务失败不会伪装成功。
6. `/health/live`、`/health/ready` 语义不同且稳定。
7. 生产认证默认开启。
8. 高风险操作无确认不能执行。
9. 备份包含三个关键数据库和仓库数据。
10. 恢复到临时目录后应用可以启动并读取关键事实。

---

## 实现状态与记录

### 实现状态：P3-1/P3-2 基础能力完成，业务 API 已接入基础入口

### 已完成交付物

| 文件 | 能力 | 测试 |
|---|---|---|
| `biz/tasks.py` | BusinessTaskDefinition/Request/JobRun/Lock、统一任务注册执行、状态转换、租约锁、stale run 回收 | `tests/test_biz_tasks.py`（8） |
| `biz/platform.py` | Health live/ready/details、SQLite 文件头与 quick_check、BackupService、checksum/manifest、统一 API 错误结构 | `tests/test_biz_platform.py`（9） |

### 验证结果

- 业务专项测试：通过。
- 容器 P0 真实数据冒烟：通过。
- 业务专项测试：`134 passed`。
- 完整回归：当前工作树 `381 passed, 1 failed`，唯一失败来自未提交的数据平面任务配置变更，非本业务平台改动。

### 已知问题

1. `tests/test_task_run_semantics.py` 的失败来自未提交的数据平面配置改动：`config/tasks/stock_daily_capture.yaml` 等文件将 `enabled` 从 `false` 改为 `true`。该改动不属于本业务平台提交，未回退、未提交；待数据平面配置变更完成配套测试更新或确认后处理。
2. BackupService 当前提供 SQLite 备份与恢复检查；仓库目录、配置和密钥的完整备份编排尚未接入业务 API。

### 后续待开发

- BusinessScheduler 与 Web 路由接入。
- 统一业务任务结果 DTO、HTTP 202 和持久化队列。
- 启动恢复检查、任务影响确认、生产级 ready 依赖检查。

### 跨模块验证

- `tests/test_biz_end_to_end.py` 作为业务闭环验收测试已通过，验证业务对象可以在隔离 business.db 中串联。
- `tests/test_biz_tasks.py` 已验证锁覆盖 handler 执行期、非 owner 不得释放锁和 stale run 回收。
- 容器内已确认新业务蓝图注册 24 个业务 API 路由；完整业务页面、正式 Scheduler、持久化 Worker 队列和生产切换流程仍待接入。
- `biz/task_registry.py` 已注册 Contracts §9 定义的 9 类业务任务，`/api/biz/tasks/<task_key>/runs` 和 `/api/biz/tasks/runs` 已提供基础运行/查询入口。
- `biz/scheduler.py` 已提供业务 APScheduler 适配器：调度回调只创建 BusinessRequest/BusinessJobRun，Worker 通过 `run_next()` 执行。
- `web/biz_api.py` 已接入新业务蓝图，当前提供筛选预览、正式筛选运行、研究运行、模拟运行、观察查询、建仓确认和建仓录入基础入口；完整业务页面、Scheduler 和 Worker 仍待接入。
- 业务任务基础 API 已接入；长任务接口现在先持久化 BusinessRequest/BusinessJobRun，再由 `run-next` Worker 领取执行。业务调度适配器已有单测，尚未挂入生产 Scheduler。
- `web/biz_api.py` 已提供 `/api/biz/health/live`、`/health/ready`、`/health/details`，与平台 HealthService 对齐。
- `tests/test_biz_tasks.py` 已验证 Request/JobRun 分离、Worker 执行、租约锁和失败恢复。
