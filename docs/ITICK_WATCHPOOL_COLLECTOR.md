# iTick 观察池独立分钟捞取

## 用途

这是一个独立的数据捞取任务，不接入现有 `warehouse/minute.py`、MinuteStore、Published Dataset、质量校验或策略计算流程。

它只读取当前统一观察池：持仓、自选和策略候选股票，然后从 iTick 获取 1 分钟 OHLCV，独立写入：

```text
output/itick_watchpool/YYYY-MM-DD/<代码>_<市场>_1m.csv
```

示例：

```text
output/itick_watchpool/2026-09-05/000400_SZ_1m.csv
```

字段为：

```text
date,instrument,open,high,low,close,volume,amount
```

## 定时任务

设置：

```text
ITICK_WATCHPOOL_ENABLED=1
```

服务启动后，在 A 股交易时段每分钟运行一次。每轮默认抓取 4 只股票，股票之间间隔 15 秒，按游标轮询整个观察池。这样不会超过 iTick 免费 REST API 每分钟 5 次的限制。

可选配置：

```text
ITICK_WATCHPOOL_BATCH_SIZE=4
ITICK_WATCHPOOL_INTERVAL_SECONDS=15
ITICK_API_KEY_FILE=/home/ubuntu/.config/itick/api_key
```

`ITICK_WATCHPOOL_BATCH_SIZE` 最大按 5 处理，不建议调大。观察池超过 4 只时，一轮不会覆盖全部股票，而是后续分钟继续轮询。

## 手工执行

当前观察池捞取下一批：

```bash
python -c 'from itick_watchpool import collect_watchpool_once; print(collect_watchpool_once())'
```

明确指定交易日和股票列表：

```bash
python -c 'from itick_watchpool import collect_watchpool_batch; print(collect_watchpool_batch(["000400.SZ","601318.SH"], day="2026-09-04"))'
```

## 说明

- 每次请求最多取约 500 根 1 分钟 bar；历史数据需要用截止时间分页，定时任务只取当前明确交易日。
- 本任务只捞取和落盘，不做完整性、OHLC 合法性、缺分钟或复权校验。
- 请求失败会记录到返回结果和日志，不会把失败标记成成功。
- API Key 只从本机私有文件读取，不写入项目文件。
- 会员到期前如需历史补采，使用明确的日期和股票列表分段调用，不要让定时任务隐式回拉历史。
