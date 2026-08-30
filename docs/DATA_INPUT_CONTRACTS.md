# 业务数据输入契约 V1

## 1. 适用范围

本文以当前数据模块的实际实现为准，定义新业务模块可以使用什么输入、通过什么接口读取、哪些字段可以假设、哪些字段不能假设。

本文不重新设计数据生产链路，也不把尚未完成统一发布闭环的数据集伪装成已完成能力。

## 2. 统一正式读取入口

对已经接入 Published Dataset 的业务读取，统一使用：

```python
from StockInvestmentTool.warehouse.datasets import load_dataset

result = load_dataset(
    warehouse,
    dataset_name,
    start_date=None,
    end_date=None,
    symbols=None,
    required_quality="WARNING",
    allow_legacy=False,
)
```

返回对象是：

```text
DatasetResult
├── data: pandas.DataFrame
└── context: dict
```

当前真实位置：`warehouse/datasets.py:14-17, 150-155`。

正式业务模块必须使用：

```text
result.data
result.context
```

不得直接扫描 Parquet、查询 `dataset_current` 或自行选择数据源。

## 3. DatasetResult.context

当前上下文字段实际包括：

```text
dataset
partition_versions
partitions
max_date
quality_status
fallback_used
```

分区上下文包括：

```text
version_id
quality_status
sources
input_versions
generated_at
```

业务模块需要注意：`max_date` 当前是在日期过滤前根据已加载数据计算的，不能直接等价为返回 DataFrame 的最大日期。业务层应对 `result.data` 再计算实际返回日期，并同时保留请求范围。

## 4. 当前可正式使用的数据集

### 4.1 stock_daily

读取：

```text
load_dataset(warehouse, "stock_daily", allow_legacy=False)
```

主键：

```text
date + code
```

当前可以依赖的基础字段：

```text
date
code
open
high
low
close
volume
amount
turn
tradestatus
```

当前不能无条件依赖：

```text
pre_close
pe_ttm
pb_mrq
```

原因：配置声明和实际已发布 Parquet 仍存在差异，最新数据中 `pre_close` 可能为空，估值字段不稳定或不在该数据集实际文件中。

业务模块必须先进行必需列校验：

```text
date/code/open/high/low/close/volume/amount
```

### 4.2 indicators

读取：

```text
load_dataset(warehouse, "indicators", allow_legacy=False)
```

个股历史的专用读取接口：

```python
warehouse.read_indicator_code(code, days=750)
```

当前常用指标字段：

```text
date
code
close
ma5
ma10
ma20
ma60
ma120
ma240
rsi14
macd
atr14
volatility_20
pct_chg
vol_ratio
ret_5d
ret_20d
high_20d
low_20d
bias_ratio
```

配置中声明但不能保证每个文件都有的字段：

```text
ma17
ma63
take_profit_reference
dual_ma_low
amplitude_abs
custom_example
```

窗口指标在历史起始阶段可以为空；业务不能把空值当作零。

### 4.3 运行时指标计算

当业务需要基于已读取行情计算指标时，使用：

```python
IndicatorRegistry.compute(df, names)
IndicatorRegistry.latest(df, names)
IndicatorContext(df)
```

这些接口只负责在 DataFrame 上计算，不负责 Published 版本、质量和数据源。调用方必须先通过 `load_dataset` 获得数据上下文，再将 DataFrame 注入指标计算。

## 5. 尚未统一的辅助数据集

以下数据集当前不能当作与 `stock_daily`、`indicators` 同等稳定的统一业务输入。

### 5.1 valuation_daily

配置字段：

```text
pe_ttm
pb_mrq
```

实际文件字段：

```text
peTTM
pbMRQ
```

在数据模块完成字段归一前，业务模块必须使用明确的标准化步骤；不能直接把配置字段名当作实际列名。估值覆盖范围也小于行情覆盖范围，缺失估值不能等价为估值通过。

### 5.2 fundamentals

当前实际读取主要使用：

```python
warehouse.read_fundamentals(code)
```

当前可保守依赖：

```text
stat_date
roe
gross_margin
```

以下字段必须按文件实际 schema 校验：

```text
code
debt_ratio
asset_liability_ratio
net_profit
revenue
net_assets
operating_cashflow
```

同一目录存在不同 schema，不能直接做全市场宽表假设。

### 5.3 industry

当前基础读取主要来自：

```python
warehouse.get_industry(code)
```

基础 `instruments.industry` 与正式 `industry` snapshot 不是同一事实。当前正式 industry 发布覆盖不足，不能作为全市场行业筛选输入。

### 5.4 money_flow_daily

当前数据主要记录为来源 Raw/Snapshot，业务不能假设配置声明路径与实际文件路径一致，也不能假设覆盖完整 Universe。

在正式读取契约补齐前，资金流只能作为显式标注来源的候选输入，不得作为正式策略必要条件。

## 6. Universe 输入

当前不存在完整统一的 `Universe` Python 对象。业务运行必须显式固化：

```text
symbols
symbol_count
asset_types
universe_id
universe_fingerprint
as_of
```

当前实际来源可能是：

```text
调用方显式 symbols
instruments 表
stock_daily 实际 code 集合
```

不同数据集覆盖不同，不能把一个数据集的 `expected_symbols` 作为另一个数据集的覆盖基准。

## 7. DataContext 组装规则

虽然当前代码返回的是普通 `context` 字典而非 `DataContext` 类，新业务统一在服务边界组装以下标准结构：

```text
DataContext
├── dataset_refs
├── indicator_refs
├── requested_start
├── requested_end
├── returned_start
├── returned_end
├── quality_status
├── source
├── fallback_used
└── is_stale
```

该结构是业务服务的标准 DTO，不得反向要求数据模块重新实现一套数据读取类。

## 8. 业务模块约束

### 选股

```text
DatasetAccess(stock_daily/indicators)
→ Screen Compiler
→ RuleRegistry exact evaluation
```

### 个股研究

```text
DatasetResult.data/context
→ IndicatorContext(df)
→ MarketRegime / RuleRegistry
```

### 回测/模拟

```text
固定日期范围的 stock_daily DatasetResult
→ 指标计算
→ StrategyContext
```

### 持仓估值

```text
Published stock_daily
→ PositionValuationService
```

### 正式策略

缺少正式 Published 数据或必需字段时必须失败或明确 partial，不能静默走旧文件、在线源或空值。

## 9. 数据模块未完成事项

以下事项完成前，相关业务能力不得标记为完整：

1. `valuation_daily` 字段归一到标准命名。
2. `fundamentals` schema 统一并可按 DatasetAccess 稳定读取。
3. `industry` 具备有效的正式全量或明确覆盖契约。
4. `money_flow_daily` 路径、周期和单位统一。
5. `DatasetResult.context.max_date` 与返回数据范围语义明确。
6. `stock_daily` 的 `pre_close`、估值字段和 YAML 达成一致。
