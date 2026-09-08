# 旧体系下线与新体系切换计划

文档状态：目标切换方案。当前尚未完成切换；“新体系唯一运行时真源”是切换完成后的验收目标，不是当前状态描述。

## 1. 原则

切换完成后，新体系是唯一运行时真源。旧体系不做长期适配、不做双写、不做双读、不做 fallback；在切换前允许保留只读迁移输入和隔离验证路径，但不得把过渡状态误报为已完成切换。

```text
旧体系 = 一次性历史数据输入
新体系 = 唯一运行时系统
```

## 2. 禁止事项

```text
新旧模型双写
新旧模型双读
新模型失败回退旧模型
旧模型继续产生策略决策
旧成对交易继续作为模拟输出
旧持仓表继续作为现金或持仓事实
旧 RuleContext/旧状态机继续参与正式计算（禁止）
```

## 3. 切换前

1. 冻结旧体系代码和配置，不再新增功能。
2. 完成生产数据库、配置和数据文件备份。
3. 运行一次性历史数据评估，区分可迁移、可重建和不可确定数据。
4. 在全新目标表中执行一次性迁移。
5. 生成迁移报告和对账报告。
6. 对账未通过时不得切换新系统。

## 3.1 开发过渡期运行模式

全重写期间允许“旧系统在线、新系统离线开发”，但不允许新旧系统同时对外提供同一业务能力。

```text
生产流量 → 旧系统
新系统   → 独立数据库、独立端口或离线测试环境
```

要求：

1. 新系统开发阶段不得写生产业务库。
2. 新系统不得被旧系统调用。
3. 新系统不得向用户展示为正式结果。
4. 数据模块继续按既有生产方式运行，不因业务重写停止。
5. 新系统通过固定测试数据和只读 Published Dataset 验证。
6. 新旧系统之间不做实时双写或实时双读。

切换前必须停止旧业务入口，并执行一次性迁移；切换后才允许新系统接收正式流量。

## 4. 数据迁移原则

目标表只由迁移程序写入，旧表只读。

```text
旧成对 SimulationTrade（仅用于一次性迁移）
→ 单边 SimulationFill BUY/SELL
→ 新 Lot 配对和收益重算
```

无法还原的字段标记：

```text
unknown
reconstructed
```

不得根据当前页面状态伪造历史事件、策略版本或交易原因。

## 5. 切换时

1. 停止旧 Scheduler、旧 Worker 和旧 API 写入口。
2. 完成最后一次迁移和校验。
3. 新系统启用新数据库和新表。
4. 所有业务读写切换到新服务。
5. `MANAGEMENT_DB_PATH` 已稳定指向生产 `management.db`；但这只证明默认路径，仍需完成生产入口扫描和隔离冷启动。
6. 旧任务台账、旧元数据库和旧业务入口是否停止产生运行时事实，必须以运行时审计和数据库写入观察证明，不得仅凭配置推断。
7. 新系统执行一条完整冒烟链路：

```text
ScreenRun
→ ResearchRun
→ StrategyDecision
→ SimulationRun
→ Observation
→ PositionCycle
→ Execution
→ Advice
→ Notification
```

## 6. 切换后

1. 旧表、旧 API、旧 Scheduler 和旧 Worker 下线。
2. 旧数据目录改为只读归档区。
3. 新系统代码不得引用旧对象名称。
4. 旧文档标记废弃，不作为开发输入。
5. 运行一段观察期后，按人工确认删除旧归档。

## 7. 切换验收

必须证明：

1. 新系统不依赖旧数据库存在。
2. 新系统不会写旧表。
3. 新系统只产生单边模拟成交。
4. 新系统只产生一种 StrategyDecision。
5. 新系统只使用一种收益核算口径。
6. 旧系统停止后核心产品仍可运行。
7. 历史迁移差异都有明确处理结论。
8. 关键策略收益已完成旧结果对账，或明确标记为重新建立基线。
9. 迁移后的所有业务代码均使用 canonical code `sh600908` 形式。
10. `published_path`、`candidate_path` 和 `rollback_path` 均为相对 Warehouse Root 的路径。
11. `index_daily/sh000300` 已发布；未完成前所有比较结果为 unavailable/partial。
12. `warehouse/meta.db` 的全部生产代码引用已清理，测试和迁移工具已改为显式使用目标管理库或只读归档输入。
13. 删除 `meta.db` 前已完成备份、引用扫描、管理库与旧库对账、隔离环境冷启动、全量回归和生产只读观察；删除动作另行人工确认。

## 8. 回测基线处理

旧引擎和新引擎的收益不默认相等。新策略、成交、费用和状态口径改变后，旧结果不能直接作为新结果展示。

必须对固定样本执行：

```text
相同数据区间
相同初始资金
相同策略参数
相同交易成本
→ 新引擎重新运行
→ 与旧结果比较
```

差异分类：

```text
数据差异
成交时点差异
费用/滑点差异
状态机差异
算法差异
历史数据不可重建
```

新系统正式上线后只展示新基线。旧结果仅作为迁移报告中的对账材料，不进入新收益排名和正式复盘。

## 8.1 当前迁移实现

- `biz/migration.py` 提供 `portfolio.db` 的只读评估和显式目标库导入，并重建基础 Account/Portfolio、INITIAL 现金、PositionCycle、Execution、PositionLot、CashLedger 和 WatchSubscription。
- 迁移使用 `legacy_entity_map` 记录旧类型、旧 ID、新类型、新 ID、迁移版本和分类。
- 源 `portfolio.db` 不删除、不写入；目标只能是调用方显式创建的 `BusinessDB`。
- 旧代码格式统一转换为 canonical code；无法转换的记录标记为 `unknown`，不写入新的业务事实表。
- 旧交易和持仓无法完整还原的策略、历史状态及建议关系标记为 `reconstructed`，不伪造精确历史事实。
- 当前仅完成临时库验证，未对生产 `portfolio.db` 执行迁移，未执行旧表删除或生产切换。
- 新数据管理库已在验证目录和生产配置中可用；旧库仍可能出现在显式 legacy、测试和迁移工具中，生产运行时引用清理和归档验收尚未完成，不得直接删除。

## 9. 实施优先级

### P0：能运行的核心闭环

```text
新库 Schema
→ StrategyContext / StrategyDecision
→ 单边 SimulationFill
→ ScreenRun / ScreenCandidate
→ ResearchRun
```

### P1：投资流程闭环

```text
Observation
→ EntryPlan
→ PositionCycle / Execution
→ CashLedger
→ PositionSnapshot
```

### P2：反馈和触达

```text
Performance / Review
→ Advice
→ NotificationEvent
→ Email
```

### P3：平台治理

```text
Task Runtime
→ Scheduler
→ Recovery
→ Health
→ Backup
```

P0 完成并通过端到端冒烟前，不扩展高级策略、复杂通知和多账户能力。
