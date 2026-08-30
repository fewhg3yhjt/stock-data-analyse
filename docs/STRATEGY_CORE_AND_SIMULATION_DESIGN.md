# 策略核心与回测模拟子模块设计 V1

## 1. 目标

本模块是整个产品的第一优先级业务核心，负责把指标条件组织成可执行策略，并使用同一套策略执行回测和模拟。

本模块不负责：

- 数据采集和数据质量治理；
- 页面布局；
- 真实交易入账；
- 邮件发送；
- 持仓最终展示。

本模块使用新定义的 `IndicatorContext`、`RuleRegistry`、`SchemeConfig` 和 `StrategyContext` 作为唯一执行基础。不得保留旧规则、旧信号或旧回测协议。

## 2. 设计原则

### 2.1 计算分层

```text
Indicator
→ Condition
→ Rule
→ Strategy
→ StrategyDecision
```

### 2.2 运行分层

```text
StrategyDecision
    ├── ScreenEvaluator
    ├── ResearchEvaluator
    ├── SimulationExecutor
    └── LiveAdviceEvaluator
```

策略不关心执行场景；执行器负责提供上下文、推进时间和处理结果。

### 2.3 版本不可变

一旦策略版本被用于模拟、建议或交易记录，策略配置不得原地修改。修改必须创建新版本。

## 3. 核心领域对象

### 3.1 IndicatorRef

```text
name
version
value_type
```

指标名称由新指标目录和新 `IndicatorContext` 定义。本模块不自行注册第二套指标名。

运行时关系：

```text
IndicatorContext = 新指标取值和序列上下文
RuleRegistry = 新条件/规则校验与执行注册中心
SchemeConfig = 新策略版本配置载体
CompiledStrategy = 新 SchemeConfig 解析后的内存运行对象
```

### 3.2 ConditionSpec

`ConditionSpec` 是传给新 `RuleRegistry` 的结构化配置，不是独立执行引擎。条件支持第一版以下类型：

```text
comparison
cross
between
consecutive
count
and
or
not
```

示例：

```json
{
  "type": "comparison",
  "left": {"field": "close"},
  "operator": ">",
  "right": {"indicator": "ma60"}
}
```

```json
{
  "type": "cross",
  "left": {"indicator": "ma20"},
  "direction": "above",
  "right": {"indicator": "ma60"}
}
```

```json
{
  "type": "consecutive",
  "condition": {"field": "close", "operator": ">", "right": {"indicator": "ma60"}},
  "days": 3
}
```

每个条件评估必须返回：

```text
passed
actual_values
threshold_values
explanation
dependencies
```

### 3.3 RuleSpec

规则将条件和动作绑定：

```json
{
  "rule_id": "pullback_entry",
  "when": {"type": "and", "conditions": [...]},
  "action": "BUY",
  "position_ratio": 0.2,
  "priority": 100,
  "valid_for": "next_open"
}
```

第一版动作：

```text
WATCH
BUY
BUY_MORE
SELL_PARTIAL
SELL_ALL
HOLD
WAIT
NO_ACTION
```

### 3.4 StrategySpec

`StrategySpec` 仅作为文档中的配置结构示例。新系统实际持久化只使用不可变的 `SchemeConfig` 策略版本，不新增另一套存储模型。

```json
{
  "strategy_id": "trend_pullback",
  "name": "趋势回踩",
  "version": 1,
  "universe": {"screen_id": "trend_candidates"},
  "entry_rules": [...],
  "exit_rules": [...],
  "risk_rules": {
    "hard_stop_ratio": 0.08,
    "max_position_ratio": 0.3
  },
  "position_sizing": {
    "mode": "fixed_ratio",
    "initial_ratio": 0.2
  },
  "execution_rules": {
    "signal_at": "close",
    "execute_at": "next_open"
  }
}
```

### 3.5 StrategyDecision

```text
decision_id
strategy_id
strategy_version
symbol
decision_time
data_as_of
action
quantity_ratio
price
stop_price
target_price
triggered_rules
input_dependencies
input_snapshot
decision_trace
reason
valid_until
```

规则冲突时按以下顺序处理：

1. 风险和强制退出；
2. 全部卖出；
3. 部分卖出；
4. 加仓；
5. 首次买入；
6. 持有和等待。

同一时点同一标的最终只输出一个主动作，同时保留完整规则评估轨迹。

`decision_trace` 的最小结构为：

```text
evaluated_rules
triggered_rules
suppressed_rules
final_action
```

## 4. 策略校验

保存或发布策略前必须校验：

1. 所有指标存在且可用于当前资产类型。
2. 所有条件的字段和操作符合法。
3. 条件没有循环依赖。
4. 规则优先级没有冲突。
5. 买入仓位比例在 `0..1` 内。
6. 全部买入比例不超过最大仓位。
7. 止损和止盈参数合理。
8. 入场和出场规则至少各有一条，除非策略被声明为纯选股策略。
9. 执行时间和价格类型合法。
10. 当前策略可以被目标执行器消费。

校验输出：

```text
valid
errors
warnings
dependencies
config_hash
```

## 5. 策略执行接口

建议提供以下领域接口，具体类名可按项目现有风格调整：

```python
strategy = StrategyDefinition.from_spec(spec)
decision = strategy.evaluate(context)
```

`context` 至少包含：

```text
symbol
evaluation_time
data_as_of
market_data
indicator_values
fundamental_values
position_state
cash_state
previous_decisions
```

策略执行不得自行读取文件、数据库或网络。所有输入由新 `StrategyContext` 注入。

## 6. 回测/模拟统一模型

### 6.1 SimulationPlan

```text
plan_id
strategy_version
symbols / universe
start_date
end_date
initial_cash
position_sizing
execution_rules
cost_config
benchmark
```

### 6.2 SimulationRun

```text
run_id
plan_id
strategy_version
data_context
status
started_at
finished_at
error
```

状态：

```text
requested
running
success
partial_success
failed
cancelled
```

### 6.3 SimulationAccount

```text
cash
positions
equity
total_fees
total_slippage
```

### 6.4 SimulationExecution

```text
execution_id
run_id
symbol
side
signal_time
execution_time
signal_price
execution_price
quantity
gross_amount
fee
tax
slippage
decision_id
reason
```

### 6.5 SimulationEvent

记录非成交事件：

```text
SIGNAL_GENERATED
ORDER_ACCEPTED
ORDER_REJECTED
FILLED
STOP_TRIGGERED
TAKE_PROFIT_TRIGGERED
END_OF_PERIOD
DATA_GAP
```

### 6.6 SimulationResult

```text
run_id
initial_cash
final_equity
total_return
benchmark_return
excess_return
max_drawdown
win_rate
profit_factor
trade_count
average_holding_days
fees
slippage
equity_curve
comparison_status
```

策略收益和基准收益分开计算。基准数据不可用时，策略结果仍可保存，`benchmark_return` 和 `excess_return` 为空，`comparison_status=unavailable`，运行整体为 `partial_success`；不能把基准缺失误报为策略失败。

### 6.7 ParameterSearchRun

参数搜索属于模拟之上的研究层，不属于模拟内核：

```text
ParameterSearchRun
    → SimulationRun 1..N
    → ParameterSearchResult
```

每组参数必须产生独立的策略配置 hash 或不可变策略版本。参数搜索直接由 `ParameterSearchRunner` 实现，不能直接修改同一个 SimulationRun。

## 7. 统一模拟执行流程

```text
锁定 StrategyVersion
→ 锁定 DataContext
→ 初始化虚拟账户
→ 按交易日读取行情
→ 计算指标
→ 生成 StrategyDecision
→ 根据 ExecutionRule 生成虚拟成交
→ 更新现金和虚拟持仓
→ 记录 SimulationExecution/Event
→ 计算每日权益
→ 计算基准和绩效
→ 保存 SimulationResult
```

### 7.1 信号日和成交日

第一版默认：

```text
收盘计算信号
→ 下一交易日开盘成交
```

策略可以配置其他执行方式，但必须明确：

- 信号时间；
- 成交时间；
- 使用开盘、收盘或限价；
- 无法成交时如何处理。

### 7.2 数据不足

数据不足不能自动当成买入失败或成功。必须记录：

```text
DATA_GAP
missing_fields
missing_period
affected_symbol
```

运行整体状态按策略配置决定是 `partial_success` 还是 `failed`。

## 8. 与现有能力的关系

新系统模块边界如下；旧实现不进入运行时：

| 当前能力 | 目标归属 |
|---|---|
| 新指标目录 | Indicator provider |
| 新 `IndicatorContext` | StrategyContext 的指标输入 |
| 新 `RuleRegistry` | Rule catalog and validator |
| 新策略评估器 | Entry/Exit rule execution |
| 新参数搜索器 | ParameterSearchRunner |
| 新 `SimulationExecutor` | 唯一回测/模拟内核 |
| 新 `MarketRegime` | 统一市场状态事实 |

旧算法不进入运行时。需要的业务能力按新协议直接重写，统一输出 `StrategyDecision`、`SimulationExecution` 和 `SimulationResult`。

## 9. 第一版实现顺序

### Step 1：定义新协议

实现并测试：


1. `ConditionSpec` 配置解析并接入 `RuleRegistry`。
2. `RuleSpec` 配置解析并接入 `RuleRegistry`。
3. `SchemeConfig` 版本校验，不新增平行 `StrategySpec` 存储。
4. `CompiledStrategy`。
5. `StrategyDecision`。
6. `StrategyValidator`。
7. `StrategyContext`。

使用一个最小趋势回踩策略作为示例，不要先改所有页面。

### Step 2：接入现有指标

要求：

1. 统一指标名称。
2. 通过数据访问层获取已发布数据。
3. 指标缺失返回明确错误，不返回假值。
4. 保存指标版本和输入数据上下文。

### Step 3：实现最小策略执行器

至少支持：

```text
comparison
cross
AND / OR
BUY
SELL_ALL
HOLD
fixed position ratio
hard stop
next open execution
```

### Step 4：实现统一 SimulationExecutor

先支持：

- 单股票；
- 固定初始资金；
- 固定仓位比例；
- 买入和卖出；
- 止损；
- 止盈；
- 手续费；
- 滑点；
- 权益曲线；
- 一个基准指数。

SimulationExecutor 只负责执行一组确定参数。网格搜索由 ParameterSearchRunner 负责生成多组 SimulationRun 并汇总比较。

### Step 5：迁移现有回测入口

先实现一个正式策略入口并调用新执行器；旧 V4.5、V6 和操作点位入口直接下线，不再进入新系统。

### Step 6：提供业务调用接口

最小接口：

```text
validate_strategy(spec)
evaluate_strategy(strategy_version, context)
create_simulation_plan(input)
run_simulation(plan_id)
get_simulation_run(run_id)
get_simulation_result(run_id)
```

## 10. 必须补充的测试

### 10.1 条件测试

1. 比较条件正确处理数值和空值。
2. 交叉条件不使用未来数据。
3. 连续 N 日条件边界正确。
4. AND / OR / NOT 结果正确。
5. 条件解释包含实际值和阈值。

### 10.2 策略测试

1. 同一输入产生确定性结果。
2. 风险退出优先于买入。
3. 同一标的同一时点只有一个主动作。
4. 策略版本 hash 稳定。
5. 非法参数无法发布。
6. 缺少指标时失败明确。

### 10.3 模拟测试

1. 收盘信号在下一交易日开盘成交。
2. 交易成本进入现金和收益。
3. 滑点进入成交价和收益。
4. 止损和止盈生成正确事件。
5. 现金不足时拒绝成交并记录原因。
6. 模拟不会修改真实 portfolio 数据。
7. 相同计划重复运行结果一致。
8. 数据版本和策略版本写入 Run。
9. 数据缺口产生 `DATA_GAP`。
10. 权益曲线和最终收益可由交易明细重算。

### 10.4 防未来数据测试

1. 条件评估只能读取评估日期及之前的数据。
2. 缓存返回严格裁剪到请求区间。
3. 指标窗口不能包含未来日期。
4. 回测结果不能读取结束日期之后的数据。

## 11. 验收标准

给定以下策略：

```text
close > ma60
且 ma20 上穿 ma60
买入 20%
跌破硬止损卖出
```

系统必须能够：

1. 校验策略并生成稳定版本。
2. 对单只股票生成可解释的 `BUY/HOLD/SELL` 决策。
3. 在指定历史区间执行模拟。
4. 生成交易明细和权益曲线。
5. 计算总收益、最大回撤和胜率。
6. 与指定基准比较。
7. 返回策略版本、数据版本和数据截止日。
8. 同一输入重复运行得到相同结果。
9. 该结果可以被持仓建议和通知模块直接消费。

## 12. 后续模块依赖

完成本模块后，其他模块必须依赖以下稳定接口：

```text
选股       → ConditionEvaluator / StrategyVersion
个股研究   → StrategyEvaluator
观察池     → StrategyDecision / SimulationResult
持仓建议   → LiveAdviceEvaluator
收益复盘   → SimulationResult / StrategyDecision / Execution
消息通知   → StrategyDecision / Advice / SimulationResult
```

任何新业务模块如果需要重新实现买卖条件，应视为设计违规，必须先扩展策略核心协议。
