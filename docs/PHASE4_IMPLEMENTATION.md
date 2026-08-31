# StockInvestmentTool Phase 4 实现指南

> 本文是后续开发执行手册。新会话开始时先阅读本文，再阅读 `IMPLEMENTATION_GUIDE.md`、`STATUS.md` 和 `PHASE4_DESIGN.md`。
> 项目路径：`/opt/stock_data_analyse`
> 生产服务：容器 `stock-invest`，Compose 服务 `stock-web`
> 生产部署：代码改动后 `sudo docker compose restart stock-web`
> 本文不授权执行密码轮换、密钥轮换、权限变更、生产恢复或历史数据删除。

## 1. 接手规则

### 1.1 工作前检查

```bash
git status --short
python3 -m pytest tests/ -q
```

必须先确认：

- 是否有其他未提交改动；
- 是否正在使用生产数据目录；
- 本任务是否会修改 `.env`、`output/` 或真实通知配置；
- 当前代码是否已经存在目标能力，避免重复造轮子。

### 1.2 修改原则

- 先写能在旧实现上失败的回归测试；
- 只做一个可验收主题；
- 不把 UI 重构、数据迁移和算法改动混在同一提交；
- 不改变既有天级回测语义，除非先建立旧结果基线；
- 新抽象必须有生产调用方；
- 不用日志文本作为唯一状态来源；
- 不用空值、0 或静默 fallback 掩盖配置错误；
- 不删除现有运行时数据；
- 测试使用 `tmp_path`、临时 SQLite 和 mock provider。

### 1.3 完成一个批次后的固定流程

```bash
python3 -m pytest tests/ -q
git add <本批次相关文件>
sudo docker inspect stock-invest --format '{{.State.Status}} {{.State.Health.Status}}'
```

最后通过生产域名验证页面/API。纯代码改动不需要 rebuild；修改依赖或 Dockerfile 才允许 build。

## 2. 当前基线

当前已知基线：

- 测试通过数量以当前测试收集结果和 CI 为准；本文件不维护固定历史数字。
- 核心业务模板均继承 `base.html`；
- `daily` 分区约 37 个；
- `minute` 分区按交易日存储；
- `NotificationOutbox` 有 pending/sent/dead 状态；
- `JobRunStore` 已记录 scheduler 任务；
- 健康接口：`GET /api/health/details`；
- 生产容器使用代码 bind mount，重启即可生效。

每次开发后测试数量可能变化，不能在文档中硬编码旧数字而不重新运行测试。

## 3. Phase 4 总批次

按以下顺序执行。每个批次完成后都要测试、提交、部署，再进入下一个批次。

```text
P0-A 数据状态服务和数据中心
P0-B 任务台账页面和手动任务
P0-C 系统告警
P1-A 导航重构和工作台
P1-B 观察池合并
P1-C 研究中心和方案发布
P1-D 通知中心完善
P2-A DataSource/Registry 完整收敛
P2-B 性能、CI、依赖和发布治理
```

## 4. P0-A 数据状态服务和数据中心

### 4.1 目标

解决“历史数据只到某天，但用户不知道有没有采集”的问题。不要先写页面，先提供统一数据状态服务。

### 4.2 建议文件

新增：

```text
ops/freshness.py
ops/health.py
web/routes/data_center.py  # 如果尚未拆 route，可先在 web/app.py 实现后再迁移
web/templates/data_center.html
tests/test_freshness.py
```

### 4.3 数据状态计算

建议 API：

```python
class DatasetStatus:
    dataset: str
    latest_value: str | None
    status: str
    last_success_at: str | None
    last_failure_at: str | None
    last_error: str | None
    rows: int | None
    symbols: int | None
    source: str | None
```

实现函数：

```python
```

数据来源：

- daily：`Warehouse.available_months("daily")`、manifest、实际 Parquet 最大日期；
- indicators/factors：对应分区文件和最大日期；
- online：`Warehouse.online_snapshots(day)` 最新文件和 `snapshot_time`；
- minute：`MinuteStore.days()`、分钟文件最大 `time`；
- 任务：`JobRunStore.recent()`；
- outbox：`NotificationOutbox.counts()`。

不要只使用 manifest。必须在必要时用实际文件校验，防止 manifest 和文件内容不一致。

### 4.4 接口

建议：

```text
GET /api/data/status
GET /api/data/jobs
GET /data-center
```

`/api/data/status` 返回：

```json
{
  "status": "success",
  "expected_trade_day": "2026-08-26",
  "datasets": [
    {
      "dataset": "daily",
      "latest_value": "2026-08-21",
      "status": "critical",
      "last_success_at": "...",
      "last_error": "WAREHOUSE_DAILY_SYNC 未开启"
    }
  ]
}
```

### 4.5 测试

至少覆盖：

- 最新日期等于预期交易日；
- 落后一个交易日；
- 落后两个交易日；
- 空数据；
- 任务未启用；
- 最近任务失败；
- minute 交易时段内 15 分钟无更新；
- 指标日期晚于 daily；
- 临时目录不影响生产 `daily`。

## 5. P0-B 任务台账页面和手动任务

### 5.1 目标

把已经存在的 `JobRunStore` 从 API 内部状态变成用户可以查看、可以追踪的任务中心。

### 5.2 接口

```text
GET /api/data/jobs?limit=50&job_name=daily_tasks&status=failed
POST /api/data/jobs/daily-sync
POST /api/data/jobs/minute-snapshot
POST /api/data/jobs/rebuild-indicators
POST /api/data/jobs/rebuild-factors
```

手动任务接口必须：

- 返回 `run_id`；
- 记录 running；
- 成功或失败后更新同一个 run；
- 防止同一 job 重复启动；
- 不直接在请求线程执行长任务；
- 提供状态查询。

### 5.3 页面

`data_center.html` 中包括：

- 数据集卡片；
- 最近任务表；
- 状态筛选；
- 错误详情；
- 手动操作按钮；
- 任务进行中轮询；
- 成功后自动刷新数据状态。

按钮文字必须说明影响范围，例如“运行日线增量同步”，不能叫“同步”而不说明是全量还是增量。

### 5.4 重要限制

- daily 同步只能调用已有增量同步服务；
- 指标重建失败时保留旧指标分区；
- 因子重建失败时保留旧因子分区；
- 不允许页面提供“清空 daily”按钮；
- 任何数据 reset 继续走现有破坏性确认规范。

## 6. P0-C 系统告警

### 6.1 目标

把“系统有没有正常运行”纳入通知系统，与股票业务通知区分 topic。

### 6.2 告警规则

```text
daily_stale       daily 落后至少 1 个交易日
daily_critical    daily 落后至少 2 个交易日
indicator_stale   indicators 晚于 daily
factor_stale      factors 晚于 daily
minute_stale      交易时段内超过 15 分钟没有分钟数据
job_failed        关键任务最近一次失败
outbox_backlog    pending 超过阈值
outbox_dead       dead 数量增加
scheduler_missing scheduler 未启用或没有预期任务
```

### 6.3 实现建议

新增：

```text
notifier/system_alerts.py
tests/test_system_alerts.py
```

系统告警也通过 `NotificationFragment` 进入 Digest/outbox，但 topic 使用：

```text
system
```

不要复用 `TOPIC_ORDERS` 冒充系统告警。

必须有去重键：

```text
system:{alert_type}:{dataset}:{date}
```

只有状态发生变化或超过冷却窗口才重复通知。

## 7. P1-A 导航重构和工作台

### 7.1 目标导航

```text
工作台 | 市场 | 观察池 | 持仓 | 研究 | 复盘 | 系统
```

### 7.2 实施顺序

1. 先在 `_nav.html` 增加分组/二级入口，不立刻删除旧路由；
2. 增加 `/workbench`，先复用已有 DashboardService、JobRunStore 和数据状态服务；
3. 将 `/` 重定向或迁移到工作台前，检查所有模板、API 和外部链接；
4. 给旧入口增加兼容跳转；
5. 通过页面 DOM 测试确认所有旧核心功能仍可达；
6. 用户确认后再隐藏旧的平级入口。

### 7.3 工作台接口

```text
GET /api/workbench/summary
GET /workbench
```

聚合内容：

- `market_summary`；
- `actionable_positions`；
- `watchlist_changes`；
- `dataset_health`；
- `notification_health`；
- `quick_actions`。

工作台服务只聚合数据，不重复实现持仓、行情和通知业务逻辑。

## 8. P1-B 观察池合并

### 8.1 目标

把观察、自选、模拟合成一个页面，但保持既有数据库表和 API 兼容。

### 8.2 视图模型

```python
{
  "code": "sh600900",
  "name": "...",
  "sources": ["manual", "strategy", "holding"],
  "watch": {...},
  "simulation": {...} | None,
  "holding": {...} | None,
  "next_action": "simulate" | "buy" | "review" | "none"
}
```

### 8.3 路由

建议新增：

```text
GET /watch-pool
GET /api/watch-pool
```

旧路由暂时保留：

```text
/dashboard/observe
/watchlist
/simulation
```

旧页面可以跳转到对应 Tab，但不能直接删除旧 API。

### 8.4 测试

- 同一股票多个来源只展示一行；
- 持仓同步不重复；
- 模拟记录正确关联；
- 删除观察来源不误删持仓；
- 行内“进入持仓”仍走事务服务；
- 旧页面跳转正确。

## 9. P1-C 研究中心和方案发布

### 9.1 目标

让指标、策略实验、编排器和方案管理形成一个产品闭环。

### 9.2 方案发布模型

建议在用户方案状态中增加：

```text
state: draft | validated | published | disabled
validated_at
validated_by
validation_sample_code
validation_result
published_at
```

如果继续使用 `.state.json`，必须保持原子写入，并与现有 scheme version 文件兼容；不要直接改写内置方案。

### 9.3 发布前验证

新增：

```text
POST /api/schemes/validate
POST /api/schemes/publish
GET  /api/schemes/impact?name=...
```

`publish` 必须拒绝：

- 未通过 schema；
- 未通过样本回测；
- 未注册规则；
- 未知指标；
- 参数未被 executor 消费；
- 买入比例不等于 1。

### 9.4 需要注意的兼容问题

- 现有 `scheme_snapshot` 继续锁定历史持仓；
- 发布新方案不自动修改历史持仓；
- 只有新建持仓或用户明确切换时才应用新版本；
- 默认方案变更必须显示影响范围。

## 10. P1-D 通知中心完善

已有：

- outbox；
- retry；
- dead；
- 每日 Digest；
- 投递台账 API。

需要继续补：

- topic 筛选；
- channel 筛选；
- status 筛选；
- 时间范围；
- 单条重试；
- 批量重试；
- dead 标记忽略；
- 发送详情；
- 系统告警 topic。

重试接口建议：

```text
POST /api/notify/outbox/<id>/retry
POST /api/notify/outbox/retry-dead
```

默认不删除记录。重试必须有最大次数和审计信息。

## 11. P2-A 完整架构收敛

### 11.1 Registry

目标：

```text
scheme rule
  → RuleRegistry
  → executor(ctx, params)
  → backtest/advisor
```

检查方法：

```bash
rg 'find_buy_rule|find_sell_rule' strategy backtest portfolio core
```

配置读取可以保留兼容 helper，但业务执行不能依赖多个固定字符串分支。

必须增加：

- 新增测试规则只注册 executor 即可运行；
- executor 真正消费 params；
- V4.5 与 V6 旧结果基线；
- Advisor 与 backtest 的关键决策一致性测试。

### 11.2 IndicatorContext

目标：

- 技术面、支撑位、策略阈值和通知指标使用同一求值入口；
- 不支持的表达式不得静默返回 0；
- 旧列名通过兼容映射处理；
- V6 的必要指标逐步迁移，不一次性重写全部指标算法。

### 11.3 DataSource

目标：

- 日线、分钟、快照、基本面和必要指数数据均有明确接口；
- Dashboard 不直接实例化具体 provider；
- Provider fallback 在 adapter 内实现；
- 业务层只依赖接口；
- 测试可注入 fake source。

## 12. P2-B 性能、CI、依赖和发布

### 12.1 性能基线

使用：

```bash
python3 scripts/benchmark_data.py sh600900 --days 750 --repeat 3
```

后续增加：

- `/api/stock/detail` P50/P95；
- `/api/data/status` P50/P95；
- 观察池首屏；
- 持仓页首屏；
- daily 增量同步耗时；
- 分钟采集耗时。

不要直接把某次机器耗时写成固定验收值，先记录环境、数据量和重复次数。

### 12.2 CI

现有 CI 已覆盖：

- Python 3.11；
- 安装 requirements；
- pytest；
- Docker build。

继续增加：

- `python -m compileall`；
- `git diff --check`；
- secret scan；
- dependency scan；
- 模板渲染 smoke；
- 临时 SQLite backup/restore smoke。

### 12.3 依赖锁定

输入文件：

```text
requirements.in
requirements.txt
requirements-dev.txt
scripts/compile_requirements.sh
```

生成 `requirements.lock.txt` 前先确认 pip-tools 可用。锁定文件生成后必须在 CI 中使用，并确认 Python 3.11 与生产镜像一致。

### 12.4 发布

当前发布约束：

- 代码挂载；
- 纯代码改动 restart；
- `output/` 持久化；
- 不使用 `down -v`；
- 不把用户数据打进镜像。

后续可增加：

- 发布前测试门禁；
- 容器健康失败自动回滚；
- 版本号和 Git SHA 页面展示；
- scheduler 版本和 job 列表展示。

## 13. 验收清单

### 数据中心

- [ ] 能看到 daily 最新交易日；
- [ ] 能看到 minute 最新时间；
- [ ] 能看到 indicators/factors 是否晚于 daily；
- [ ] 能看到最近一次任务状态；
- [ ] 失败原因可读；
- [ ] 手动任务返回 run id；
- [ ] 页面能轮询任务状态；
- [ ] 不修改既有 daily 内容。

### 工作台和导航

- [ ] 一级入口不超过 7 个；
- [ ] 工作台展示今日市场、持仓、观察和系统状态；
- [ ] 观察/自选/模拟可从一个页面完成；
- [ ] 研究入口包含指标、策略实验和方案编排；
- [ ] 旧路由兼容；
- [ ] 桌面和移动端验证。

### 通知

- [ ] 业务通知和系统告警分 topic；
- [ ] outbox 状态可查；
- [ ] pending/sent/retry/dead 可区分；
- [ ] 单条死信可重试；
- [ ] 多源盘后 Digest 不重复发送；
- [ ] 系统故障可以通知。

### 策略

- [ ] 指标 → 策略 → 验证 → 发布闭环可操作；
- [ ] 未验证方案不可发布；
- [ ] 发布影响范围可见；
- [ ] 历史持仓 snapshot 不被覆盖；
- [ ] 编排器配置与真实引擎行为一致；
- [ ] V4.5/V6 基线测试存在。

### 工程

- [ ] 完整测试通过；
- [ ] Docker build CI 通过；
- [ ] 性能基线已记录；
- [ ] Parquet 原子写入测试通过；
- [ ] scheduler 单实例测试通过；
- [ ] 生产核心页面 HTTP 200；
- [ ] 生产容器 healthy；
- [ ] Git 工作区干净。

## 14. 后续模型汇报格式

每个批次完成后必须汇报：

```text
批次：P0-A / P1-B / ...

已实现：
- 生产入口
- 新增/修改的数据模型
- 新增接口和页面

生产调用链：
- 修改前
- 修改后
- 旧路径如何兼容或停用

测试证据：
- 新增测试
- 完整测试结果
- 关键行为结果

部署证据：
- commit
- 容器状态
- 页面/API 冒烟

未完成和风险：
- 明确列出，不使用“基本完成”掩盖缺口
```

## 15. 当前建议的第一个执行批次

新会话接手后，严格从 P0-A 开始：

1. 实现 `ops/freshness.py`；
2. 读取 daily/indicator/factor/online/minute 实际数据；
3. 结合 `JobRunStore` 和环境开关计算状态；
4. 新增 `/api/data/status`；
5. 新增 `data_center.html`；
6. 增加数据新鲜度测试和页面 DOM 测试；
7. 运行完整测试；
8. 提交并部署；
9. 在生产页面确认“日线只到 21 号”的原因可以被用户直接看见。

不要先改首页导航，也不要先删除旧入口。先让数据状态可解释，再进入工作台和信息架构重构。
