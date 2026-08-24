# -*- coding: utf-8 -*-
"""基本面采集 — 行业 + 完整财务史 进数据层

采集目标:
  ① 行业: 每只股票 → meta.db instruments.industry 列
  ② 财务史: 每只股票完整财务（ROE/毛利率/扣非/负债率/现金流等）→
     warehouse/fundamentals/<code>.parquet

设计:
  - 行业低频静态，采集一次基本到位（带缓存跳过已采集）
  - 财务史季度静态，财报披露后更新
  - 复用 fetcher 的 get_stock_industry / get_fundamental_history（含网络缓存）
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd

from StockInvestmentTool.datasource.fetcher import StockDataFetcher
from StockInvestmentTool.warehouse.storage import Warehouse

logger = logging.getLogger(__name__)


class FundamentalsCollector:
    """行业 + 财务史 采集器（进数据层）。"""

    def __init__(self, warehouse: Optional[Warehouse] = None,
                 fetcher: Optional[StockDataFetcher] = None):
        self.warehouse = warehouse or Warehouse()
        self._fetcher = fetcher

    @property
    def fetcher(self) -> StockDataFetcher:
        if self._fetcher is None:
            self._fetcher = StockDataFetcher()
        return self._fetcher

    # ── 行业采集（meta.db）────────────────────────────

    def collect_industry(self, codes: Optional[list[str]] = None,
                         max_symbols: Optional[int] = None,
                         refresh_all: bool = False) -> dict:
        """全市场行业采集，写入 meta.db instruments.industry。

        refresh_all=False 时跳过已采集的（industry 非空）。
        """
        if codes is None:
            codes = self.warehouse.all_codes()
        if max_symbols:
            codes = codes[:max_symbols]

        done = updated = skipped = 0
        t0 = time.time()
        for i, code in enumerate(codes, 1):
            if not refresh_all and self.warehouse.get_industry(code):
                skipped += 1
                continue
            try:
                ind = self.fetcher.get_stock_industry(code)
                if ind:
                    self.warehouse.update_industry(code, ind)
                    updated += 1
                done += 1
            except Exception as e:
                logger.warning("行业采集 %s 失败: %s", code, e)
            if i % 500 == 0 or i == len(codes):
                logger.info("行业进度 %d/%d, 已更新 %d", i, len(codes), updated)
        logger.info("行业采集完成: 更新 %d, 跳过 %d, 耗时 %.1fs",
                    updated, skipped, time.time() - t0)
        return {"updated": updated, "skipped": skipped,
                "elapsed_sec": round(time.time() - t0, 1)}

    # ── 财务史采集（fundamentals 分区）─────────────────

    def collect_fundamentals(self, codes: Optional[list[str]] = None,
                             max_symbols: Optional[int] = None,
                             years: int = 5) -> dict:
        """全市场财务史采集，写入 warehouse/fundamentals/<code>.parquet。

        已采集（has_fundamentals）的跳过（财务史季度静态，财报更新后重采）。
        """
        if codes is None:
            codes = self.warehouse.all_codes()
        if max_symbols:
            codes = codes[:max_symbols]

        done = skipped = failed = 0
        t0 = time.time()
        for i, code in enumerate(codes, 1):
            if self.warehouse.has_fundamentals(code):
                skipped += 1
                continue
            try:
                df = self.fetcher.get_fundamental_history(code, years=years)
                if df is not None and not df.empty:
                    self.warehouse.write_fundamentals(code, df)
                    done += 1
                else:
                    skipped += 1
            except Exception as e:
                failed += 1
                logger.warning("财务史采集 %s 失败: %s", code, e)
            if i % 200 == 0 or i == len(codes):
                logger.info("财务史进度 %d/%d, 完成 %d", i, len(codes), done)
        logger.info("财务史采集完成: 完成 %d, 跳过 %d, 失败 %d, 耗时 %.1fs",
                    done, skipped, failed, time.time() - t0)
        return {"done": done, "skipped": skipped, "failed": failed,
                "elapsed_sec": round(time.time() - t0, 1)}