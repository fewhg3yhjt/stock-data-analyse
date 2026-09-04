# 公开市场数据 API

> 对外只读接口，面向在线大模型、API 工具和外部应用。服务地址：`https://www.stock.easyconnect.ltd`。

## 从哪里开始

这套接口不需要 API Key，不需要登录，只提供 `GET` 查询。它不触发采集、不写入数据，也不返回采集任务、原始 HTTP 响应或内部文件路径。

数据来自已发布的 Published Dataset。也就是说，数据已经经过现有的候选构建、质量检查和发布流程；尚未发布、质量不达标或采集失败的内容不会通过此 API 暴露。

为兼容只识别固定路径的 HTTP 客户端，也提供简化入口：

```text
GET https://www.stock.easyconnect.ltd/public-api
GET https://www.stock.easyconnect.ltd/public-api/health
GET https://www.stock.easyconnect.ltd/public-api/stock/daily?code=000400&start=2025-09-01&end=2026-09-04&adjust=qfq
```

`/public-api` 和 `/public-api/health` 均直接返回 JSON；人类可读的网页文档和在线查询器位于 `https://www.stock.easyconnect.ltd/public-api/docs`。

让大模型接入时，先请求下面两个地址即可发现能力：

```text
GET https://www.stock.easyconnect.ltd/api/public/openapi.json
GET https://www.stock.easyconnect.ltd/api/public/reports
```

`openapi.json` 提供接口清单，`reports` 返回当前真实存在的数据报告、字段、已发布分区和最新分区。调用方应以 `reports` 返回结果为准，不应假设某个历史日期一定有数据。

## 通用响应

列表查询返回相同的分页外层结构：

```json
{
  "status": "ok",
  "report": "industry_daily",
  "start": "2026-05-07",
  "end": "2026-09-03",
  "page": 1,
  "page_size": 100,
  "total": 7470,
  "total_pages": 75,
  "has_next": true,
  "has_prev": false,
  "data": [],
  "meta": {
    "data_as_of": "2026-09-03",
    "quality_status": "PASS",
    "source": "published_dataset",
    "partition_versions": {
      "2026-09": "industry_daily_202609_example"
    }
  }
}
```

`meta` 仅在来自 Published Dataset 的报告中出现：

| 字段 | 含义 |
|---|---|
| `data_as_of` | 本次实际返回数据的最新业务日期或快照日期 |
| `quality_status` | 发布版本的质量状态，例如 `PASS` 或 `WARNING` |
| `source` | 固定为 `published_dataset`，表示没有读取未发布原始批次 |
| `partition_versions` | 返回数据所使用的已发布分区与版本标识 |

空结果仍返回 `200` 和空数组 `data: []`。参数不合法时返回 `400`；指定范围没有可读取的已发布数据集时返回 `404`。

## 分页与限制

所有列表接口均支持：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `page` | `1` | 从 1 开始的页码 |
| `page_size` | `100` | 单页记录数，范围 `1` 到 `1000` |

当 `has_next` 为 `true` 时，使用相同条件请求下一页，例如 `page=2`。请勿把 `page_size` 设为超过 `1000`。

股票代码可使用存储中的标准形式，如 `sh600000`、`sz000001`；查询时也接受带点的写法，如 `sh.600000`、`sz.000001`。

## 数据发现

### `GET /api/public/reports`

列出全部公开报告、字段和已发布分区。适合让大模型先知道“有哪些数据”。

```text
https://www.stock.easyconnect.ltd/api/public/reports
```

每个报告会包含：

- `report`：报告标识，例如 `stock_daily`、`industry_daily`
- `display_name`、`description`：中文说明
- `path`：对应 API 路径
- `fields`：当前报告字段名
- `published_partitions`、`latest_published_partition`：可读取分区

### `GET /api/public/openapi.json`

提供 OpenAPI 3.0.3 文档入口。可用于 API Connector、Action 或 MCP 包装层：

```text
https://www.stock.easyconnect.ltd/api/public/openapi.json
```

## 股票目录

### `GET /api/public/stocks`

返回当前采集的标的基础资料，包括股票、ETF 和指数。数据来自标的目录，不是行情数据。

| 参数 | 说明 |
|---|---|
| `symbol` | 精确筛选代码，例如 `sh600000` 或 `sh.600000` |
| `q` | 按代码或名称模糊搜索，例如 `茅台` |
| `type` | 按标的类型筛选，例如 `stock`、`etf`、`index` |
| `page`、`page_size` | 通用分页参数 |

```text
https://www.stock.easyconnect.ltd/api/public/stocks?q=贵州茅台&page=1&page_size=20
```

常见字段：`code`、`name`、`type`、`board`、`listed_date`、`industry`、`updated_at`。

## 板块与成员关系

板块数据保留分类口径和来源，不会将不同来源的行业静默合并。

当前可能的 `classification`：

| 值 | 说明 | 常见来源 |
|---|---|---|
| `csrc` | 证监会行业分类 | `baostock` |
| `ths_industry` | 同花顺行业分类 | 已发布行业成员快照 |

后续若新增概念、地域等公开成员数据集，仍会以独立 `classification` 返回。

### `GET /api/public/sectors`

返回已发布成员快照中的板块目录。

| 参数 | 说明 |
|---|---|
| `as_of` | 可选快照日期，格式 `YYYY-MM-DD`；省略时读取各分类当前最新已发布快照 |
| `classification` | 按分类体系筛选，例如 `csrc` |
| `source` | 按来源筛选，例如 `baostock` |
| `page`、`page_size` | 通用分页参数 |

```text
https://www.stock.easyconnect.ltd/api/public/sectors?classification=ths_industry&page=1&page_size=100
```

返回字段：`sector_id`、`sector_name`、`classification`、`source`、`snapshot_date`、`captured_at`。

### `GET /api/public/stock-sectors`

返回股票和板块之间的对应关系。一只股票归属多个板块时会返回多条记录。

| 参数 | 说明 |
|---|---|
| `symbol` | 按股票代码筛选 |
| `sector_id` | 按板块代码筛选 |
| `classification` | 按分类体系筛选 |
| `as_of` | 可选快照日期，格式 `YYYY-MM-DD` |
| `page`、`page_size` | 通用分页参数 |

查询某股票的板块：

```text
https://www.stock.easyconnect.ltd/api/public/stock-sectors?symbol=sh600000&page=1&page_size=100
```

查询某板块中的股票：

```text
https://www.stock.easyconnect.ltd/api/public/stock-sectors?sector_id=881101&classification=ths_industry&page=1&page_size=100
```

返回字段随来源略有差异，公共字段包括：`symbol`、`sector_id`、`sector_name`、`classification`、`snapshot_date`、`source`、`captured_at`。同花顺行业成员数据还会带 `stock_name`。

## 个股日线

### 简化兼容接口：`GET /public-api/stock/daily`

这是面向 `curl`、Python requests 和只识别固定路径的大模型工具的直接 JSON 接口。不需要 Cookie、登录、认证头或浏览器 JavaScript。

| 参数 | 必填 | 说明 |
|---|---|---|
| `code` | 是 | 支持 `000400`、`000400.SZ`、`sz000400` 等代码形式 |
| `start` | 是 | 起始交易日期，`YYYY-MM-DD` |
| `end` | 是 | 结束交易日期，`YYYY-MM-DD` |
| `adjust` | 否 | 固定支持 `qfq`，默认 `qfq`；当前未发布 `none`、`hfq` 口径时会明确返回 `422` |

```text
https://www.stock.easyconnect.ltd/public-api/stock/daily?code=000400&start=2025-09-01&end=2026-09-04&adjust=qfq
```

返回结构：

```json
{
  "status": "ok",
  "code": "000400",
  "name": "许继电气",
  "adjust": "qfq",
  "start_date": "2025-09-01",
  "end_date": "2026-09-04",
  "count": 244,
  "data": [
    {
      "date": "2025-09-01",
      "open": 22.87,
      "high": 23.2,
      "low": 22.73,
      "close": 22.81,
      "volume": 25953400,
      "amount": 606226800,
      "pre_close": null,
      "turnover_rate": 2.57
    }
  ]
}
```

`data` 不做分页，会完整返回所请求日期范围内该股票的所有已发布交易日记录。请求一年区间时会返回完整的一年交易日，而不是只取最近几十条。

### `GET /api/public/stock-daily`

查询已发布的股票、ETF 或指数日线。`start` 和 `end` 都是必填项，接口不会猜测日期范围或回退到默认历史区间。

| 参数 | 必填 | 说明 |
|---|---|---|
| `start` | 是 | 起始交易日期，`YYYY-MM-DD` |
| `end` | 是 | 结束交易日期，`YYYY-MM-DD` |
| `symbol` | 否 | 股票代码；省略时返回日期范围内所有已发布标的，通常不建议这样调用 |
| `page`、`page_size` | 否 | 通用分页参数 |

```text
https://www.stock.easyconnect.ltd/api/public/stock-daily?symbol=sh600000&start=2026-08-01&end=2026-09-03&page=1&page_size=100
```

主要字段：`date`、`code`、`open`、`high`、`low`、`close`、`pre_close`、`volume`、`amount`、`turn`、`tradestatus`。

## 行业日线

### `GET /api/public/industry-daily`

查询已发布的同花顺行业指数日线。与个股日线一样，`start` 和 `end` 均为必填参数。

| 参数 | 必填 | 说明 |
|---|---|---|
| `start` | 是 | 起始交易日期，`YYYY-MM-DD` |
| `end` | 是 | 结束交易日期，`YYYY-MM-DD` |
| `industry_id` | 否 | 行业板块代码，例如 `881101` |
| `page`、`page_size` | 否 | 通用分页参数 |

```text
https://www.stock.easyconnect.ltd/api/public/industry-daily?industry_id=881101&start=2026-05-07&end=2026-09-03&page=1&page_size=100
```

单条记录示例：

```json
{
  "trading_date": "2026-05-07",
  "industry_name": "种植业与林业",
  "industry_id": "881101",
  "open": 4949.318,
  "high": 4966.253,
  "low": 4934.655,
  "close": 4955.415,
  "volume": 1024802690,
  "amount": 8586866700.0,
  "source": "akshare",
  "source_symbol": "种植业与林业",
  "captured_at": "2026-09-02T05:48:53"
}
```

## 给大模型的调用建议

模型应按下面的顺序工作：

1. 先请求 `/api/public/reports`，确认报告、字段和已发布日期范围。
2. 查询名称或代码未知的标的时，调用 `/api/public/stocks?q=关键词`。
3. 需要股票所属板块时，调用 `/api/public/stock-sectors?symbol=代码`；需要板块成分股时改用 `sector_id`。
4. 需要价格或指数分析时，明确传入 `start`、`end` 与代码，再按 `has_next` 翻页。
5. 分析结论应引用返回记录中的 `trading_date`、`snapshot_date`、`source` 和 `meta.data_as_of`，不应把采集时间 `captured_at` 误当作交易日期。

示例指令：

```text
先读取 https://www.stock.easyconnect.ltd/api/public/reports，确认可用字段和数据日期。
然后查询 sh600000 最近一个月的个股日线和所属行业，基于实际返回数据说明趋势；
如数据不完整或没有对应记录，明确说明，不要编造或用其他日期替代。
```

## 跨域与访问边界

- API 响应包含 `Access-Control-Allow-Origin: *`，浏览器页面和在线工具可跨域读取。
- API 无认证、无写接口，任何人均可查询公开的已发布业务数据。
- 接口不提供实时行情保证，数据新鲜度以 `meta.data_as_of` 和记录中的业务日期为准。
- 请控制请求频率并使用分页。接口设置单页最多 `1000` 条，避免一次请求下载全量行情。
- 业务接口不等同于投资建议。外部调用方应自行核验数据、市场状态和适用性。
