# -*- coding: utf-8 -*-
"""warehouse — 全量数据仓库

离线全量采集（baostock 日线/财务史）+ Parquet 月分区存储 + 因子宽表 +
DuckDB 全市场扫描 + 在线观察池快照。

模块:
    - storage.py   存储层（Parquet 分区 + SQLite 元数据清单）
    - collector.py 离线全量采集（代码清单 + 日线增量）
    - factors.py   因子宽表计算（月度分块）
    - scanner.py   DuckDB 全市场扫描
    - online.py    观察池盘中快照
    - cli.py       CLI 入口
"""

from StockInvestmentTool.warehouse.storage import Warehouse
from StockInvestmentTool.warehouse.collector import MarketCollector
from StockInvestmentTool.warehouse.factors import FactorEngine
from StockInvestmentTool.warehouse.scanner import MarketScanner
from StockInvestmentTool.warehouse.process import ProcessEngine
from StockInvestmentTool.warehouse.backfill import ValuationBackfill

__all__ = ["Warehouse", "MarketCollector", "FactorEngine", "MarketScanner",
           "ProcessEngine", "ValuationBackfill"]