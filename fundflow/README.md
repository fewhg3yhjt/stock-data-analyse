# A股资金流分析模块 fundflow

短线视角的资金流向工具：**价格是资金流动的结果**，跟踪主力净额的方向与持续性，
判断资金"正在流向哪里"、是否价涨钱随（配合）还是价涨钱走（背离）。

数据源：同花顺资金流（不依赖易被封的东财接口）。

## 快速开始

```bash
# 全量分析（大盘概况 + 个股/行业/概念 × 即时 + 3日趋势）
python -m StockInvestmentTool.fundflow

# 加 5 日趋势对比
python -m StockInvestmentTool.fundflow --trend-days 3,5

# 只看个股，只打印不落盘
python -m StockInvestmentTool.fundflow --sections stock --no-export

# 榜单条数
python -m StockInvestmentTool.fundflow --top 20
```

退出码: `0` 成功；`1` 数据拉取失败。

## 输出

`output/fundflow/fundflow_YYYYMMDD_HHMMSS.*`：

| 文件 | 内容 |
|---|---|
| `industry_now.csv` / `industry_3d.csv` | 90 行业即时 + 近3日累计净额 |
| `concept_now.csv` / `concept_3d.csv` | 387 概念（同上） |
| `stock_now.csv` | 全市场个股即时资金流（5000+ 只） |
| `stock_净流入榜/净流出榜/持续流入榜/价涨钱走榜.csv` | 个股分析榜单 |
| `fundflow_*.json` | 汇总（大盘概况 + 全部数据），供下游程序消费 |

## 分析逻辑

- **趋势标签**（今日净额 vs 近N日累计净额 的符号）：
  `持续流入` / `持续流出` / `转为流入` / `转为流出` / `无方向`
- **背离检测**：涨跌幅>0 且 主力净额<0 →「价涨钱走」，短线警示
- **配合检测**：涨跌幅>0 且 主力净额>0 →「价涨钱随」，强势信号
- **持续流入榜**：今日 + 近N日均净流入，排除单日脉冲

## 数据源与口径（重要）

同花顺资金流三接口 × 周期：
- 行业 `stock_fund_flow_industry` / 概念 `stock_fund_flow_concept`
- 个股 `stock_fund_flow_individual`（全市场，即时/多日）

**单位坑（已实测校准）**：
- 板块表（行业/概念）即时：无后缀数字 = **亿**
- 板块表多日：全部无后缀 = **亿**
- 个股表即时：无后缀数字 = **元**（净额极小的票），有后缀按 万/亿
- 个股表多日（列名不同）：`阶段涨跌幅`/`连续换手率`/`资金流入净额`（净额带万/亿后缀）

东财 `stock_individual_fund_flow_rank` 等接口在大陆住宅宽带会被连接级风控，
故默认不启用；解封后可作个股超大单/大单细分的补充源。

## 目录结构

```
fundflow/
├── sources.py    # 数据源适配器（同花顺 + 单位解析）
├── analysis.py   # 趋势标签 / 背离检测 / 榜单
├── export.py     # CSV/JSON 导出
├── cli.py        # 命令行入口
└── __main__.py
```

测试: `PYTHONPATH=repo根目录 python -m pytest StockInvestmentTool/tests/test_fundflow.py`
（离线，合成数据驱动纯逻辑。）
