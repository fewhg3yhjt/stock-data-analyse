# 数据平台统一设计 V2

## 1. 定位与边界

数据平台是所有业务模块的事实输入层，负责把外部来源变成可校验、可追溯、可按统一接口消费的数据集。

```text
Source
→ Raw Batch
→ Normalized Dataset
→ Quality Result
→ Published Dataset
→ DatasetAccess
→ DatasetResult(data, context)
```

本设计以当前已经存在的实现为基础：

```text
DatasetAccess
DatasetResult
IndicatorRegistry
IndicatorContext
IndicatorsBuilder
management.db 中的数据版本和质量表
```

不另造第二套数据访问框架，不让业务模块直接扫描 Parquet、读取 Raw、访问外部源或自行处理字段别名。

## 2. 数据平面职责

### 2.1 数据平面负责

1. 外部数据源访问和限流。
2. 原始批次和失败记录。
3. 字段、代码、日期和单位归一。
4. 主键去重和分区写入。
5. 数据集质量检查。
6. Dataset Version、Current 和 checksum。
7. Universe Snapshot 和覆盖范围。
8. `DatasetAccess.load_dataset()`。
9. 返回真实 `DatasetResult.data/context`。

### 2.2 业务平面不负责

1. 选择数据源。
2. 处理 `peTTM`/`pe_ttm` 等外部别名。
3. 读取 Raw 文件。
4. 通过文件名推断证券代码。
5. 自行决定数据是否 Published。
6. 用“有非空数据”代替质量判断。

## 3. 统一数据接口

当前正式接口：

```python
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

返回：

```text
DatasetResult
├── data: pandas.DataFrame
└── context: dict
```

业务服务层将当前普通 `context` 规范化为：

```text
dataset
schema_version
partition_versions
dataset_versions
data_as_of
requested_start
requested_end
returned_start
returned_end
quality_status
source_batches
universe_id
symbol_count
row_count
fallback_used
warnings
```

`returned_start/returned_end` 必须从过滤后的 `data` 计算，不能直接使用过滤前的 `max_date`。

## 4. 数据集统一目录

不同数据集可以有不同粒度和物理分区，但每个正式数据集必须定义：

```text
dataset_name
schema_version
grain
primary_key
partition_strategy
storage_path
field_definitions
date_semantics
universe_definition
source_definition
quality_policy
consumer_policy
```

### 4.1 stock_daily

```text
grain: symbol + trading_date
primary_key: trading_date + symbol
partition: month
```

标准字段：

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

`pre_close`、估值字段只有在实际 Published schema 确认后才能使用。

### 4.2 indicators

```text
grain: symbol + trading_date
primary_key: trading_date + symbol
partition: month
source: Published stock_daily
```

研究因子属于指标目录中的指标，不再建立独立 `factors` 数据集。

常用字段：

```text
ma5/ma10/ma20/ma60/ma120/ma240
rsi14/macd/atr14/volatility_20
pct_chg/vol_ratio/ret_5d/ret_20d
high_20d/low_20d/bias_ratio
```

### 4.3 valuation_daily

```text
grain: symbol + trading_date
primary_key: trading_date + symbol
```

标准字段：

```text
trading_date
symbol
pe_ttm
pb_mrq
pe_status
pb_status
```

外部 `peTTM`、`pbMRQ` 只能在 Source Normalizer 中出现。覆盖不足时只能作为可选研究输入，不能伪装为全市场估值完整。

### 4.4 fundamentals

```text
grain: symbol + report_period
primary_key: symbol + report_period
```

标准字段至少包括：

```text
symbol
report_period
announcement_date
revenue
revenue_yoy
net_profit
net_profit_yoy
roe
asset_liability_ratio
operating_cashflow
net_assets
goodwill
```

历史研究只能使用：

```text
announcement_date <= research_as_of
```

文件名不能代替 `symbol` 列，`debt_ratio` 和 `asset_liability_ratio` 不能无说明互换。

### 4.5 industry_membership

行业正式数据集统一命名为 `industry_membership`，标准字段：

```text
symbol
industry_code
industry_name
classification_system
classification_version
effective_from
effective_to
```

当前行业数据未达到全量正式输入前，行业过滤能力标记为受限，不得直接使用 `instruments.industry` 冒充正式数据集。

### 4.6 money_flow_security_daily

证券资金流和行业资金流分开：

```text
money_flow_security_daily
money_flow_industry_daily
```

证券字段：

```text
trading_date
symbol
name
net_inflow
total_amount
window_days
unit
source
```

`period` 不再作为正式标准字段；窗口累计值使用 `window_days` 表达。

## 5. Universe

Universe 必须保存成员明细，而不是只保存一个批次字符串。

```text
universe_definitions
universe_snapshots
universe_members
```

`UniverseSnapshot`：

```text
universe_snapshot_id
universe_type
as_of
asset_types
member_count
fingerprint
```

`UniverseMember`：

```text
universe_snapshot_id
symbol
asset_type
status
listed_date
is_active
```

业务模块必须明确：

```text
universe_id
asset_types
include_suspended
include_delisted
as_of
```

不同数据集覆盖范围不同，不能互相作为完整覆盖基准。

## 6. 指标生产和消费

### 6.1 生产

```text
DatasetAccess(stock_daily)
→ IndicatorsBuilder
→ IndicatorRegistry.compute(df)
→ indicators partition
→ Published indicators
```

### 6.2 消费

批量业务优先读取：

```text
DatasetAccess("indicators")
```

个股业务可以使用：

```python
warehouse.read_indicator_code(code, days)
```

但正式读取必须由数据访问层补充版本、质量和日期上下文。

运行时表达式使用：

```text
IndicatorContext(result.data)
```

它只能在已经通过数据访问门禁的 DataFrame 上计算，不负责数据源和版本。

## 7. 指标定义版本

指标目录必须成为计算定义的唯一声明源：

```text
Metric Catalog
→ IndicatorDefinition
→ IndicatorVersion
→ IndicatorRegistry
→ IndicatorsBuilder
```

每个指标版本至少保存：

```text
indicator_id
version
canonical_name
definition
dependencies
formula_or_code_hash
schema_version
min_history
applies_to
status
```

每个 `indicators` Dataset Version 必须关联本次使用的指标版本集合：

```text
indicator_versions_json
input_dataset_versions_json
```

## 8. 质量门禁

通用规则：

```text
主键重复
必填字段
类型
日期
代码
空数据
checksum
```

数据集专属规则：

```text
行情：OHLC、交易日、价格
指标：窗口期、计算失败、覆盖
估值：负 PE、不可适用、覆盖
基本面：报告期、公告日、比率范围
行业：分类版本、生效期、覆盖
资金流：周期、单位、证券/行业范围
```

`validation` 小样本批次不得更新生产 Current。

## 9. 迁移顺序

1. 盘点和冻结现有数据集。
2. 修复 `valuation_daily` 字段和 Published 路径。
3. 统一 `fundamentals` schema、代码列、公告日期和报告期。
4. 将行业从 `instruments.industry` 收敛到 `industry_membership`。
5. 将资金流拆成证券和行业数据集，明确单位和窗口。
6. 建立 Universe Snapshot 成员明细。
7. 统一 DatasetAccess 对所有正式数据集的读取。
8. 删除业务侧 Raw、旧路径和在线 fallback。

## 10. 验收

1. 所有正式业务数据集都有 DatasetAccess 读取路径。
2. 业务模块不处理外部字段别名。
3. 每个数据集都有明确日期语义、Universe、schema 和质量门禁。
4. 估值、基本面、行业和资金流的缺失、不可适用、失败和未覆盖能够区分。
5. 所有指标结果都能追溯到 stock_daily 版本和指标定义版本。
6. 业务可见数据只来自 Published Dataset。
7. 数据模块改造不会创建第二套指标计算引擎。
