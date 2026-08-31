# 观察池与关注列表子模块设计 V1

## 1. 模块定位

本模块负责承接选股结果和人工关注，管理股票从“发现”到“进入策略模拟或真实建仓参考”的中间过程。

本模块要回答：

```text
这只股票为什么进入我的视野？
我是否决定持续关注它？
它当前处于观察的哪个阶段？
最近一次研究或模拟结果是什么？
下一步应该做什么？
```

本模块不负责：

- 重新实现选股条件；
- 重新实现策略买卖规则；
- 运行完整回测；
- 修改真实持仓和现金；
- 直接发送邮件。

它消费 `ScreenCandidate`、`StrategyDecision` 和 `SimulationRun`；行情和指标只通过 `DATA_PIPELINE_V1_DESIGN.md` 定义的真实数据对象进入，并向持仓模块提供建仓上下文。

## 2. 必须先区分的三个概念

当前系统容易把以下概念混为一个 `watchlist` 记录，目标设计必须分开。

### 2.1 DiscoveryCandidate：筛选候选

含义：某次选股运行中，这只股票满足了筛选条件。

特点：

- 属于一次具体筛选运行；
- 有明确的数据日期；
- 有命中条件和实际指标值；
- 可以自动过期；
- 不代表用户决定关注；
- 不代表产生买入信号。

来源：`ScreenRun` / `ScreenCandidate`。

### 2.2 WatchSubscription：用户关注关系

含义：用户主动决定长期或阶段性关注这只股票。

特点：

- 可以人工创建；
- 可以由候选转化而来；
- 有用户备注、关注目的和目标资金；
- 即使某次筛选候选过期，也可以继续存在；
- 删除关注关系不删除历史候选、研究和交易记录。

### 2.3 Observation：投资观察流程

含义：这只股票当前正在经历一段从候选、研究、模拟到建仓判断的投资观察周期。

特点：

- 有生命周期；
- 可关联多个候选来源和多个研究/模拟运行；
- 可以暂停、过期、晋级或退出；
- 是后续 `SimulationPlan` 和 `PositionCycle` 的业务来源。

目标关系：

```text
DiscoveryCandidate 0..N
        ↓
Observation 0..N
        ↑
WatchSubscription 0..N
```

第一版可以暂时使用一只股票一个 active Observation，但数据模型不要把候选、关注和观察永久合并为一个对象。

## 3. 用户主流程

```text
选股运行
    ↓
查看候选和走势
    ├── 忽略
    ├── 加入关注列表
    └── 创建观察
            ↓
      个股研究 / 选择策略
            ↓
      运行回测或模拟
            ├── 继续观察
            ├── 暂停
            ├── 放弃
            └── 形成建仓计划
                    ↓
              录入真实成交
                    ↓
              PositionCycle
```

关键原则：

```text
筛选命中 ≠ 用户关注
用户关注 ≠ 当前观察
观察完成模拟 ≠ 已经建仓
模拟通过 ≠ 真实交易已发生
```

## 4. 第一版范围

### 4.1 必须支持

1. 查看一次筛选运行的候选明细。
2. 从候选创建或更新关注关系。
3. 记录候选来源、筛选条件和数据日期。
4. 创建和查看 Observation。
5. 查看 Observation 当前状态和下一步动作。
6. 为 Observation 选择策略版本。
7. 从 Observation 创建回测/模拟计划。
8. 保存最近一次模拟运行和结果。
9. 从 Observation 生成建仓参考上下文。
10. 进入真实建仓录入，但不自动创建成交。
11. 支持人工暂停、恢复、放弃和归档。
12. 显示候选过期、数据滞后和模拟失败状态。

### 4.2 暂不支持

- 自动根据筛选结果批量建仓；
- 自动删除过期关注对象；
- 多人共享观察池；
- 复杂看板拖拽编排；
- 盘中自动晋级；
- 观察池直接执行交易；
- 用观察状态代替真实持仓状态。

## 5. 核心实体

### 5.1 DiscoveryLink

保存观察对象与候选来源的关系。

```text
link_id
observation_id
screen_run_id
screen_candidate_id
source_strategy_id
source_strategy_version
discovered_at
reason_snapshot
data_as_of
created_at
```

同一 Observation 可以有多个 DiscoveryLink，例如：

```text
市场发现命中
资金流候选命中
用户手工加入
策略扫描再次命中
```

原始链接不可覆盖，只能新增关系或新增事件。

### 5.2 WatchSubscription

用户关注关系。

```text
subscription_id
symbol
name
asset_type
purpose
target_amount
notes
status
started_at
paused_at
ended_at
created_at
updated_at
```

`purpose` 第一版：

```text
research
candidate
potential_entry
holding_mirror
```

`status`：

```text
active
paused
ended
```

关注关系结束不删除历史事实。

### 5.3 Observation

观察周期是本模块的主实体。

```text
observation_id
symbol
name
asset_type
status
current_strategy_id
current_strategy_version
observation_reason
target_amount
started_at
expires_at
latest_data_as_of
latest_simulation_run_id
latest_research_run_id
promoted_position_cycle_id
created_at
updated_at
```

### 5.4 ObservationSnapshot

保存某个观察时点的计算结果，不把当前动态页面结果当成历史事实。

```text
snapshot_id
observation_id
snapshot_time
data_as_of
strategy_id
strategy_version
price
market_state
entry_plan
stop_plan
decision_action
decision_reason
data_context
```

快照是推导结果，必须绑定策略版本和数据上下文。

### 5.5 ObservationEvent

记录观察流程中的业务事件。

```text
event_id
observation_id
event_type
event_time
from_status
to_status
source_id
reason
metadata
```

事件类型第一版：

```text
CREATED
SOURCE_LINKED
SUBSCRIPTION_CREATED
SIMULATION_STARTED
SIMULATION_COMPLETED
SIMULATION_FAILED
PAUSED
RESUMED
EXPIRED
PROMOTED_TO_POSITION
ABANDONED
ARCHIVED
```

## 6. Observation 状态机

### 6.1 状态

```text
discovered
observing
ready_for_entry
promoted
paused
expired
abandoned
archived
```

### 6.2 合法转换

```text
discovered
    → observing
    → abandoned
    → expired

observing
    → ready_for_entry
    → paused
    → expired
    → abandoned

ready_for_entry
    → promoted
    → observing
    → expired
    → abandoned

paused
    → observing
    → abandoned
    → archived

expired
    → observing        # 用户明确恢复，产生新事件
    → archived

abandoned
    → archived

promoted
    → archived         # 持仓模块接管后，观察记录只读
```

Simulation 的执行状态属于 `SimulationRun`，Observation 不重复维护 `simulating` 和 `simulated` 状态。Observation 通过 `latest_simulation_run_id` 和派生的 `simulation_status` 展示模拟进度。

状态转换必须由服务层统一执行，不允许页面直接修改状态字段。

### 6.4 Expiry 推进机制

增加轻量维护任务：

```text
observation.expiry_reconcile
```

职责：

```text
查询 active Observation
→ 检查 expires_at
→ observing/ready_for_entry → expired
→ 写 ObservationEvent
```

普通 GET 查询只返回 `effective_status=expired` 提示，不直接写数据库。维护任务是唯一持久化推进者。

### 6.3 状态语义

| 状态 | 含义 | 是否可运行模拟 | 是否可进入建仓 |
|---|---|---:|---:|
| `discovered` | 刚被发现，尚未确认关注 | 是 | 否 |
| `observing` | 用户正在关注和研究 | 是 | 否 |
| `ready_for_entry` | 用户确认可以作为建仓参考 | 是 | 是 |
| `promoted` | 已转入真实持仓流程 | 否 | 否 |
| `paused` | 暂停观察 | 否 | 否 |
| `expired` | 当前观察有效期结束 | 否 | 否 |
| `abandoned` | 用户放弃 | 否 | 否 |
| `archived` | 历史只读 | 否 | 否 |

`ready_for_entry` 只表示具备建仓参考条件，不表示已成交。

## 7. 来源和优先级

### 7.1 来源类型

```text
screen
strategy
money_flow
manual
holding
imported
```

### 7.2 来源处理规则

1. `manual` 关注不能被自动筛选结果覆盖。
2. `holding` 是持仓镜像，不等于普通观察。
3. `screen` 必须保留 `screen_run_id` 和 `candidate_id`。
4. `strategy` 必须保留策略 ID 和版本。
5. 多个来源同时存在时，页面显示来源集合，而不是选择一个覆盖其他来源。
6. 来源失效只影响对应链接，不自动删除 Observation。

### 7.3 “下一步动作”推导

`next_action` 可以作为展示字段，但必须由状态和前置条件推导：

```text
discovered      → review
observing       → research_or_simulate / evaluate_simulation_result
ready_for_entry → confirm_entry
promoted        → manage_position
paused          → resume_or_archive
expired         → renew_or_archive
abandoned       → archive
```

不能仅依据“是否存在 simulations 记录”推导业务状态。

## 8. 观察到模拟的衔接

创建模拟计划时必须携带：

```text
observation_id
symbol / universe_snapshot
strategy_id
strategy_version
source_screen_run_id
observation_snapshot_id
data_context
```

流程：

```text
Observation.observing
    → 创建 SimulationPlan
    → 由 SimulationRun 记录 requested/running
    → SimulationRun 完成
    → 保存 ObservationSnapshot
    → SimulationRun.success ? 保持 observing 并更新 simulation_status : observing/paused
```

模拟完成后不能自动把 Observation 标记为 `ready_for_entry`，必须满足：

1. 模拟运行成功；
2. 策略版本仍有效；
3. 数据质量和日期满足业务门禁；
4. 用户明确确认，或系统有明确配置允许自动晋级。

第一版采用用户明确确认。

## 9. 观察到真实持仓的衔接

从 Observation 进入建仓时，创建的是建仓意图或建仓上下文：

```text
observation_id
simulation_run_id
strategy_version
entry_plan
data_context
target_amount
```

用户录入真实成交后，持仓模块创建 `PositionCycle` 和 `Execution`，并回写：

```text
Observation.status = promoted
Observation.promoted_position_cycle_id = ...
```

如果用户只是打开建仓页面、填写草稿或取消，不得将 Observation 标记为 `promoted`。

真实建仓不会删除：

- DiscoveryCandidate；
- WatchSubscription；
- ObservationSnapshot；
- SimulationRun。

## 10. 观察池列表视图

列表必须同时展示事实和推导：

### 事实

- 股票代码和名称；
- 关注状态；
- Observation 状态；
- 来源集合；
- 观察开始时间；
- 有效期；
- 是否已经持仓。

### 推导

- 最新价格；
- 数据截至日期；
- 市场状态；
- 当前策略动作；
- 模拟收益；
- 下一步动作。

推导字段必须显示数据日期和策略版本，不能把历史快照误标成当前状态。

## 11. API 目标

### 11.1 创建关注

```text
POST /api/watch-subscriptions
```

```json
{
  "symbol": "sz000001",
  "purpose": "research",
  "target_amount": 10000,
  "notes": "关注趋势回踩机会"
}
```

### 11.2 从候选创建观察

```text
POST /api/screen-candidates/{candidate_id}/observation
```

服务端从 Candidate 读取来源、条件快照和数据上下文，不能只接收前端传来的 symbol。

### 11.3 查询观察池

```text
GET /api/observations
GET /api/observations/{observation_id}
GET /api/observations/{observation_id}/events
GET /api/observations/{observation_id}/snapshots
```

### 11.4 选择策略

```text
POST /api/observations/{observation_id}/strategy
```

必须保存策略 ID 和不可变版本，不允许只保存策略名称。

### 11.5 创建模拟计划

```text
POST /api/observations/{observation_id}/simulation-plan
```

### 11.6 状态操作

```text
POST /api/observations/{observation_id}/pause
POST /api/observations/{observation_id}/resume
POST /api/observations/{observation_id}/abandon
POST /api/observations/{observation_id}/ready-for-entry
POST /api/observations/{observation_id}/promote
```

每个操作都必须校验合法状态转换并写 `ObservationEvent`。

## 12. 错误语义

统一错误类型：

```text
OBSERVATION_NOT_FOUND
OBSERVATION_INVALID_TRANSITION
OBSERVATION_ALREADY_PROMOTED
SUBSCRIPTION_NOT_FOUND
CANDIDATE_NOT_FOUND
CANDIDATE_EXPIRED
SIMULATION_NOT_READY
SIMULATION_FAILED
STRATEGY_VERSION_UNAVAILABLE
DATA_CONTEXT_STALE
ENTRY_CONFIRMATION_REQUIRED
```

以下情况不能返回成功：

- Candidate 已过期仍尝试直接晋级；
- Observation 已 `promoted` 仍修改当前策略；
- Simulation 失败却进入 `ready_for_entry`；
- 前端伪造不存在的 Candidate 来源；
- 数据质量不满足却允许正式建仓。

## 13. 当前能力映射

| 当前能力 | 目标归属 | 处理方式 |
|---|---|---|
| 旧 `watchlist` | WatchSubscription | 一次性迁移后旧表下线 |
| `source` 字段 | DiscoveryLink / Source relation | 不再只保存单个来源字符串 |
| `sim_entry` | ObservationSnapshot / EntryPlan | 不再作为唯一模拟事实 |
| `portfolio.simulations` | 建仓分析快照 | 明确命名，避免与 SimulationRun 混淆 |
| 资金流候选 | DiscoveryCandidate | 保存运行批次和命中原因 |
| `DashboardService.watch_pool()` | Observation read model | 只负责聚合展示，不直接推进状态 |
| `next_action` | 状态派生字段 | 由服务层统一计算 |
| 持仓自动同步自选 | holding source link | 不覆盖人工关注关系 |

### 13.1 存量迁移

旧 `watchlist` 迁移为：

```text
watchlist row → WatchSubscription
strategy/money_flow source → Observation + DiscoveryLink
sim_entry → ObservationSnapshot
existing holding → holding source link + promoted reference
```

迁移程序可以使用临时日志记录旧记录与新记录的对应关系，但该日志不属于新业务模型，也不被业务服务读取。迁移幂等，不覆盖人工备注；无法判断来源的记录标记为 `manual_imported`。迁移完成后旧表和旧入口下线。

## 14. 实施步骤

### Step 1：冻结并迁移旧数据

冻结旧入口，执行一次性数据迁移：

```text
watchlist row → WatchSubscription
strategy candidate → DiscoveryLink
simulation snapshot → ObservationSnapshot
```

迁移校验完成后删除旧表、旧字段和旧入口，不建立运行时兼容层。

### Step 2：增加观察实体和事件

实现：

1. Observation；
2. DiscoveryLink；
3. ObservationEvent；
4. 状态转移服务；
5. 当前列表 read model。

### Step 3：接入选股候选

要求：

1. 候选可以创建 Observation；
2. 保留 ScreenRun 和 Candidate 来源；
3. 同一股票多个来源不互相覆盖；
4. 过期候选不能直接晋级。

### Step 4：接入策略和模拟

要求：

1. Observation 绑定 StrategyVersion；
2. SimulationPlan 绑定 Observation；
3. SimulationRun 完成后写 Snapshot；
4. 模拟失败不能晋级；
5. 用户确认后才进入 `ready_for_entry`。

### Step 5：接入持仓

要求：

1. 从 Observation 生成建仓上下文；
2. 真实成交后创建 PositionCycle；
3. Observation 变为 `promoted`；
4. 取消建仓不改变观察状态；
5. 历史观察和模拟结果保留。

### Step 6：迁移现有页面

顺序：

1. `watch_pool.html`；
2. `watchlist.html`；
3. `observe.html`；
4. `simulation.html`；
5. 市场发现结果页；
6. 建仓页面。

## 15. 测试要求

### 来源和观察

1. Candidate 创建 Observation 后保留来源快照。
2. 多个来源可以同时存在。
3. 手工关注不会被自动候选覆盖。
4. Candidate 过期不删除 WatchSubscription。
5. 删除关注不删除 DiscoveryCandidate 和 Observation 历史。

### 状态机

1. 非法转换被拒绝。
2. 同一 Observation 不能重复启动相同的 SimulationRun。
3. Simulation 失败不能进入 `ready_for_entry`。
4. 未确认不能晋级建仓。
5. 已 `promoted` 的 Observation 只读。
6. 每次状态变化都有 ObservationEvent。

### 模拟和持仓

1. SimulationPlan 固化候选集合。
2. SimulationRun 绑定策略版本和数据版本。
3. 取消建仓不创建成交。
4. 真实成交后正确关联 PositionCycle。
5. 建仓后历史观察、模拟和候选仍可查询。

### 页面和 API

1. 列表同时显示来源、状态、数据日期和下一步动作。
2. 过期数据有明确提示。
3. API 不能仅凭裸 symbol 伪造来源。
4. 观察状态和真实持仓状态不混用。
5. 重复点击晋级操作具有幂等性。

## 16. 验收标准

用户可以完成以下流程：

```text
执行选股
→ 查看某个候选的命中条件和走势图
→ 从候选创建观察对象
→ 选择一个策略版本
→ 创建并完成回测/模拟
→ 查看模拟结果和数据上下文
→ 明确确认可进入建仓
→ 录入实际成交
→ Observation 标记为 promoted
→ 生成 PositionCycle
```

同时满足：

1. 每个阶段都能查到来源和时间。
2. 筛选命中、用户关注、模拟完成和真实成交语义不混淆。
3. 取消、失败和过期不会伪造成功晋级。
4. 任何历史记录都不会因为删除当前关注关系而消失。

## 17. 后续依赖

```text
选股模块     → DiscoveryCandidate / ScreenRun
策略模块     → StrategyVersion / StrategyDecision
模拟模块     → SimulationPlan / SimulationRun
持仓模块     → PositionCycle / Execution
通知模块     → ObservationEvent / StrategyDecision
复盘模块     → DiscoveryLink / ObservationSnapshot / PositionCycle
```

---

## 实现状态与记录

### 实现状态：P1-1 基础能力完成

### 已完成交付物

| 文件 | 能力 | 测试 |
|---|---|---|
| `biz/observation.py` | Observation/WatchSubscription/DiscoveryLink、8 态状态机、状态事件、expiry_reconcile、查询与持久化 | `tests/test_biz_observation.py`（9） |

### 后续待开发

- 从 ScreenCandidate 自动建立来源链路。
- Observation 绑定策略、SimulationRun 和 ObservationSnapshot。
- 从 Observation 生成 EntryPlan 并接入真实建仓事务。
- Observation read model 与 Web API。

### 已补充

- `ObservationService.transition_and_save()` 和 `promote_and_save()` 已在同一事务内写入状态与 `ObservationEvent`。
- Observation 创建时，Observation、初始 `CREATED` 事件和 DiscoveryLink 已统一在一个事务中保存。
- `web/biz_api.py` 已提供 Observation 列表、关注关系创建、状态操作和建仓入口。
- `observation.expiry_reconcile` 已提供查询、状态推进、事件持久化的一体化入口，普通查询不产生状态副作用。
- `biz/workflow.py` 已提供候选 → Observation → SimulationPlan → EntryContext → PositionCycle/Execution 的跨模块应用服务。
- `web/biz_api.py` 已提供 `/api/observations`、`/api/watch-subscriptions` 和状态操作正式入口。
- `ObservationSnapshot` 已落库，并可由 `/api/research-runs/{run_id}/observation-snapshot` 从研究结果创建。

### 跨模块验证

- `tests/test_biz_end_to_end.py` 已覆盖 ScreenRun/ScreenCandidate → ResearchRun/StrategyDecision → Observation → SimulationPlan/SimulationRun → PositionCycle/Execution → PositionValuation → Performance/Review → Advice/NotificationDelivery。
- `tests/test_biz_workflow.py` 已覆盖 ScreenCandidate → Observation → SimulationPlan → EntryContext → PositionCycle/Execution，并验证建仓前确认、现金不足回滚和幂等重放。
- 当前测试已通过，但业务 API 和最终 Web 入口尚未接入。

### 跨模块验证

- `tests/test_biz_end_to_end.py` 已覆盖 ScreenRun/ScreenCandidate → ResearchRun/StrategyDecision → Observation → SimulationPlan/SimulationRun → PositionCycle/Execution → PositionValuation → Performance/Review → Advice/NotificationDelivery。
- 当前测试已通过，但部分跨模块关联仍由测试编排代码显式设置，正式 Application Service 尚未完全收口。
