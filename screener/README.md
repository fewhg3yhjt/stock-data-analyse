# A股初筛模块 screener

按可配置规则，把全市场 A 股初筛成「当前账户可交易 + 符合条件」的目标股票集合，
一次导出 CSV/JSON，供后续评估框架批量跑分析。

## 快速开始

```bash
# 用默认规则（screen_rules.yaml）
python -m StockInvestmentTool.screener

# 命令行覆盖（优先级: 默认 < YAML < 命令行）
python -m StockInvestmentTool.screener --boards main_sh,main_sz --max-price 35
python -m StockInvestmentTool.screener --max-pe 30 --min-total-mcap 50
python -m StockInvestmentTool.screener --rules 我的规则.yaml --export csv
```

退出码: `0` 成功（命中可为空）；`1` 数据源全部失败。

## 规则配置（screen_rules.yaml）

| 配置块 | 关键项 | 示例 |
|---|---|---|
| `universe` | 股票池源 + 备胎 | `sina` 主（一次调用全市场，最稳） |
| `boards` | 保留/剔除板块 | 只留 `[main_sh, main_sz]`（沪深主板，普通账户） |
| `name_filter` | 剔 ST/关键词 | `exclude_st: true`，`exclude_keywords: ["退"]` |
| `price` | 股价区间 | `max: 35.0` |
| `enrich` | PE/PB/市值/换手率增强 | `tencent`（批量报价，不封IP）→ 备胎 `em` |
| `valuation` | 估值/规模过滤 | `max_pe_ttm` / `max_pb` / `min_total_mcap`(亿) / `max_turnover` |
| `output` | 导出 | CSV+JSON，`max_rows` 上限 |

板块 key：`main_sh` 沪主板 / `main_sz` 深主板 / `cyb` 创业板 / `kcb` 科创板 / `bse` 北交所
（创业板/科创板/北交所需额外开通权限，示例只留沪深主板）。

## 数据流

```
股票池(新浪 5538只)
  → 板块过滤(前缀判定, ~3193只沪深主板)
  → 名称过滤(剔除ST/退)
  → 股价过滤(≤35元)
  → 腾讯批量报价增强(PE/PB/总市值/流通市值/换手率/量比/涨跌停)
  → 估值规则过滤(可选)
  → 排序导出 CSV/JSON + 同花顺行业热度TOP(参考)
```

## 输出

- `output/screener/screen_YYYYMMDD_HHMMSS.csv` — utf-8-sig，Excel 直接打开
  列: 代码 / 名称 / 板块 / 现价 / 涨跌幅% / PE(TTM) / PB / 总市值(亿) / 流通市值(亿) /
       换手率% / 量比 / 涨停价 / 跌停价 / 最高 / 最低
- 同名 `.json` — 含规则、各阶段过滤统计、命中明细，供下游程序消费

## 数据源（实测结论）

| 源 | 用途 | 可靠性 |
|---|---|---|
| 新浪 `stock_zh_a_spot` | 全市场股票池（一次调用） | ✅ 稳定 |
| 腾讯 `qt.gtimg.cn` 批量报价 | PE/PB/市值/换手率增强 | ✅ 不封IP |
| 东财 `stock_zh_a_spot_em` | 股票池备胎 / 增强备胎 | ⚠️ 大陆宽带会被连接级风控 |
| 同花顺行业汇总 | 行业热度参考（仅附注） | ✅ 可用（成分列表有403反爬，不用作股票池） |

> baostock `query_all_stock` 也可作股票池（`--universe baostock`），但无价格，需后续增强。

## 目录结构

```
screener/
├── board.py          # 板块判定（前缀 → 板块 + 权限标注）
├── rules.py          # 规则模型 + YAML 加载
├── sources.py        # 数据源适配器（新浪/东财/腾讯/同花顺/baostock）
├── pipeline.py       # 主流程（各阶段计数）
├── export.py         # CSV/JSON 导出
├── cli.py            # 命令行入口
├── __main__.py       # python -m 入口
└── screen_rules.yaml # 规则配置
```

测试: `PYTHONPATH=repo根目录 python -m pytest StockInvestmentTool/tests/test_screener.py`
（纯离线，不依赖网络；用 monkeypatch 替换数据源层。）
