# 建议与消息通知子模块设计 V1

## 1. 模块定位

本模块把策略判断和系统事件转化为可查看、可确认、可投递的建议与消息。

核心链路：

```text
StrategyDecision / SystemAlert / DailyReport
→ Advice / NotificationEvent
→ NotificationDelivery
→ Outbox
→ Email
→ 投递结果和用户确认
```

通知模块不重新计算买卖策略。策略判断由策略核心产生，通知模块只负责编排、去重、投递和记录。

Advice 的产生链路固定为：

```text
PositionSnapshot + StrategyVersion
→ LiveAdviceEvaluator
→ StrategyDecision
→ Advice
→ NotificationEvent
```

新系统直接实现 `LiveAdviceEvaluator`。旧 `portfolio/advisor.py` 不进入运行时；需要的规则按新协议重写，不能绕过 `StrategyDecision`。

## 2. 第一版范围

### 必须支持

1. 买入信号邮件。
2. 卖出信号邮件。
3. 持仓风险邮件。
4. 每日摘要邮件。
5. 任务失败和数据异常邮件。
6. SMTP 测试发送。
7. Outbox 持久化。
8. 失败重试和死信。
9. 通知去重。
10. 查看投递状态和失败原因。
11. 通知关联策略决策、建议和报告。

### 暂不支持

- 自动交易；
- 复杂多渠道编排；
- 用户分组和多租户模板；
- 任意脚本发送；
- 无审计的直接 SMTP 调用。

## 3. 核心实体

### 3.1 Advice

业务建议，通常来自持仓或策略决策。

```text
advice_id
portfolio_id
cycle_id
symbol
action
quantity
price
stop_price
target_price
reason
triggered_rules
strategy_id
strategy_version
data_as_of
valid_until
status
created_at
```

状态：

```text
generated
notified
acknowledged
accepted
ignored
expired
partially_executed
executed
cancelled
```

### 3.2 NotificationEvent

说明“为什么要发消息”。

```text
event_id
event_type
subject_type
subject_id
symbol
advice_id
strategy_decision_id
report_id
priority
dedupe_key
payload
created_at
```

事件类型：

```text
BUY_SIGNAL
SELL_SIGNAL
RISK_ALERT
DAILY_REPORT
TASK_FAILED
DATA_QUALITY_ALERT
SYSTEM_ALERT
```

### 3.3 NotificationDelivery

说明通过哪个渠道投递。

```text
delivery_id
event_id
channel
recipient
template
status
attempts
last_error
sent_at
created_at
```

状态：

```text
pending
processing
sent
failed
dead
suppressed
```

### 3.4 DailyReport

日报先结构化保存，再渲染为 Markdown、HTML 或邮件。

```text
report_id
report_date
generated_at
data_as_of
market_snapshot
observation_snapshot
portfolio_snapshot
advice_ids
sections
status
```

## 4. 通知去重

业务事件必须生成稳定 `dedupe_key`，建议组成：

```text
event_type
symbol
advice_id / strategy_decision_id
strategy_version
data_as_of
action
```

同一业务事件重复触发时：

1. 不创建重复事件；或
2. 复用原事件并更新投递状态。

不要仅通过扫描最近 N 条消息去重。

信号去重和建议更新分开处理：

```text
BusinessSignalKey = portfolio/cycle/symbol/strategy/version/signal_type
SignalOccurrenceKey = BusinessSignalKey + data_as_of + trigger_fingerprint
```

同一有效信号的价格或数量变化只更新 Advice revision，不重复发送；原 Advice 过期、执行或出现新的 trigger fingerprint 后才产生新通知。

建议增加：

```text
advice_revision
trigger_fingerprint
last_notified_revision
```

## 5. Outbox 投递流程

```text
创建 NotificationEvent
→ 创建 NotificationDelivery(pending)
→ 原子 claim(processing)
→ 渲染模板
→ SMTP 发送
→ 成功标记 sent
→ 失败记录错误并计算 next_attempt_at
→ 超过次数标记 dead
```

Claim 必须具备租约：

```text
claimed_by
claimed_at
lease_expires_at
```

租约过期后可被其他 Worker 接管。

## 6. 邮件设计

### 6.1 配置

```text
smtp_host
smtp_port
smtp_username
smtp_password
from_address
to_addresses
use_tls
connect_timeout
```

密码不得出现在普通日志、API 响应、报告和异常消息中。

### 6.2 邮件内容

邮件必须包含：

```text
标题
事件类型
股票/组合
建议动作
建议有效期
数据截至时间
策略版本
触发原因
数据来源和质量
系统链接
```

### 6.3 测试发送

测试发送必须：

1. 只允许已配置的收件人，或明确标记为测试地址。
2. 不伪造业务事件。
3. 记录测试投递结果。
4. 不能通过任意 URL 触发不受控网络请求。

## 7. Advice 与实际执行

```text
Advice.generated
→ NotificationEvent
→ notified
→ 用户 acknowledged / accepted / ignored
→ Actual Execution
→ partially_executed / executed
```

实际成交时通过 `advice_id` 关联建议，计算：

```text
建议价格 vs 实际价格
建议数量 vs 实际数量
建议时间 vs 实际时间
```

建议过期后不得再被视为当前有效建议。

## 8. 晨报和通知统一数据快照

晨报和摘要通知必须引用同一批结构化快照：

```text
data_as_of
market_snapshot
observation_snapshot
portfolio_snapshot
advice_ids
```

不能由晨报和通知各自重新调用 Advisor 并产生两套结果。

## 9. 系统告警

系统告警必须具备生命周期：

```text
detected
→ notified
→ acknowledged
→ recovered
→ closed
```

至少支持以下告警：

- 数据质量失败；
- 数据明显滞后；
- 任务失败；
- 任务长期运行；
- 通知进入 dead；
- 磁盘或内存资源异常。

告警去重使用：

```text
alert_type
resource
failure_code
active_window
```

## 10. API 目标

```text
GET  /api/advices
GET  /api/advices/{advice_id}
POST /api/advices/{advice_id}/acknowledge
POST /api/advices/{advice_id}/accept
POST /api/advices/{advice_id}/ignore
GET  /api/notifications/events
GET  /api/notifications/deliveries
POST /api/notifications/test-email
POST /api/notifications/deliveries/{id}/retry
GET  /api/reports/daily/{report_date}
GET  /api/system/alerts
POST /api/system/alerts/{id}/acknowledge
```

## 11. 当前能力映射

| 当前能力 | 目标归属 | 迁移方式 |
|---|---|---|
| 新 Advice | Advice | 新系统唯一建议实体 |
| 新 NotificationRule | NotificationRule | 新系统唯一规则实体 |
| 新 NotificationDelivery | NotificationDelivery/Outbox | 新系统唯一投递实体 |
| 新通知服务 | Template and event service | 统一事件和模板 |
| 新 DailyReportBuilder | DailyReport builder | 先生成结构化报告 |
| 新 SystemAlertService | SystemAlert service | 统一生命周期和故障码 |
| 新 ChannelService | Channel service | Email 为第一版验收渠道 |

Email 是第一版完整验收渠道；Feishu 和 WeCom 不删除，继续作为同一 Channel Adapter 接口的可用实现，不能各自维护独立业务通知逻辑。

## 12. 实施步骤

1. 统一 Advice 结构和状态。
2. 将策略决策转成 NotificationEvent。
3. 实现 Email Channel 和 SMTP 测试发送。
4. 将事件和投递写入 Outbox。
5. 增加去重和 claim/lease。
6. 将每日摘要改为结构化 DailyReport。
7. 接入任务失败、数据质量和资源告警。
8. 迁移旧通知规则，最后删除重复发送路径。

## 13. 测试与验收

1. 产生 BUY/SELL Advice 后可创建通知事件。
2. 同一 dedupe key 不重复创建或发送。
3. SMTP 成功后 Delivery 为 `sent`。
4. SMTP 失败按退避重试并最终进入 `dead`。
5. Worker 并发不会重复领取同一消息。
6. 测试邮件不会泄露 SMTP 密码。
7. 晨报和邮件使用同一 data_as_of 和 advice 集合。
8. Advice 与实际 Execution 可关联并计算偏差。
9. 系统告警可确认、恢复和关闭。
10. 发送失败不会影响交易和持仓事实。

---

## 实现状态与记录

### 实现状态：P2-2 基础能力完成，Web/任务编排待接入

### 已完成交付物

| 文件 | 能力 | 测试 |
|---|---|---|
| `biz/notification.py` | LiveAdviceEvaluator、Advice 状态机、Advice 转换、NotificationEvent 去重、NotificationDelivery、claim/lease、失败重试/dead、EmailChannel | `tests/test_biz_notification.py`（8） |

### 后续待开发

- LiveAdviceEvaluator 与 PositionValuationService/StrategyDecision 正式接入。
- DailyReportBuilder、结构化快照和系统告警生命周期。
- Feishu/WeCom Channel Adapter。
- BusinessTask `advice.refresh` 与 `notification.outbox_delivery` 调度接入。

### 跨模块验证

- `tests/test_biz_end_to_end.py` 已验证 StrategyDecision → Advice → NotificationEvent → NotificationDelivery → Email Channel 的基础投递链路。
- 当前验证使用 fake channel；LiveAdviceEvaluator、DailyReportBuilder、系统告警和正式任务调度仍待接入。

### 已补充

- `LiveAdviceEvaluator` 已实现 `StrategyContext → StrategyDecision → Advice`。
- Advice 状态转换已集中到 `NotificationService.transition_advice()`。
- 成功投递可推进关联 Advice，失败投递不会伪造通知成功。
- `record_execution` 已支持按实际执行数量推进 Advice 的 `partially_executed/executed` 状态。
- `biz/reporting.py` 已提供结构化 DailyReport 和 SystemAlert 生命周期基础服务，并接入业务 API。
- `web/biz_api.py` 已提供 Advice、NotificationEvent 和 NotificationDelivery 查询入口。
- Outbox claim 已改为数据库事务内条件更新，租约未过期时并发 Worker 不能重复领取。
