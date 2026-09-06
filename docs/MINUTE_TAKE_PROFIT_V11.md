# 分钟级止盈策略 V11

V11 直接读取现有腾讯分钟链路的当天分钟 close、volume、amount，不读取 iTick 独立输出。

当前运行模式：

```text
POSITION_V11_MODE=notify
```

只生成通知，不创建自动卖出动作。现有固定回撤逻辑仍保持原样，V11 是附加的 shadow/notify 判定。

## 当前口径

- `day_open`：当天第一条 `09:30` 之后的腾讯分钟 close。
- `running_high/running_low`：当天已出现分钟 close 的最高/最低值。
- `current_price`：当天最后一条分钟 close。
- `prev_close`、`atr_pct`：只读 T-1 及以前已发布日线。
- `drawdown_atr`：`(running_high-current_price)/ATR(T-1)`。
- 有效回撤候选：默认 `0.75 ATR`。
- EffectiveSwing：默认 `0.75 ATR`，只用分钟 close 路径压缩走势段。
- 结构确认：Lower High、Lower Low、跌破开盘、放量走弱。
- 盈利状态按止盈结构判断；亏损状态进入 `STOP_LOSS` 状态提示。

## 配置

```text
POSITION_V11_MODE=notify
POSITION_V11_CANDIDATE_ATR=0.75
POSITION_V11_SWING_ATR=0.75
POSITION_V11_CONFIRMATION_BARS=3
POSITION_V11_VOLUME_SURGE_RATIO=1.8
```

`confirmation_bars` 已保留配置入口，当前状态机先以有效分钟路径结构为主，不会自动执行卖出。

## 触发链路

腾讯分钟任务成功落盘后，现有 `PositionRuntimeService.evaluate_all()` 被调用。V11 在同一次持仓评估中读取当天分钟文件，更新 `position_runtime_states.data_context_json`，并在状态首次进入 `TAKE_PROFIT_PENDING`、`TAKE_PROFIT` 或 `STOP_LOSS` 时通过通知 outbox 生成通知。

V11 不读取 iTick 文件，不写 MinuteStore 以外的数据，不接入 Published Dataset，不改变真实持仓。

邮件订阅规则 `notification_minute_take_profit_v11` 已开启，使用 `.env` 的 `EMAIL_TO`，事件进入现有 notification outbox 后由 outbox worker 异步投递。
