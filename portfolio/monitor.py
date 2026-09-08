"""价格监控 — 拉取最新行情 + 重算技术指标

数据源策略（warehouse 优先，baostock 兜底）:
    - 优先读 warehouse 数据层（本地全量 6435 只 × 3 年，快、稳定）
    - warehouse 数据不足/缺失时，fallback 到 baostock 实时拉取
    - 这样持仓/观察池/晨报的指标计算不依赖网络，且数据统一来自数据层

提供刷新单个/全部持仓最新价的原始能力。策略判断委托给 PostPurchaseAdvisor。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from StockInvestmentTool.datasource.fetcher import StockDataFetcher
from StockInvestmentTool.datasource.indicators import TechnicalIndicators, ValuationHelper
from StockInvestmentTool.datasource.base import get_default_datasource
from StockInvestmentTool.warehouse.storage import Warehouse

logger = logging.getLogger(__name__)

# 分析窗口: 拉1年，确保 MA120/年内高点 等指标可用
DEFAULT_LOOKBACK_YEARS = 1


class PriceMonitor:
    """价格监控

    Args:
        fetcher: 数据获取器（可选，默认新建）
    """

    def __init__(self, fetcher: Optional[StockDataFetcher] = None, datasource=None):
        # 懒加载: 纯 DB 操作（list/show）不应触发 baostock 登录
        self._fetcher = fetcher
        self.datasource = datasource  # 可注入统一数据源（默认 PublishedDataSource）

    @property
    def fetcher(self) -> StockDataFetcher:
        if self._fetcher is None:
            self._fetcher = StockDataFetcher()
        return self._fetcher

    def fetch_kline(self, code: str,
                    start_date: Optional[str] = None,
                    end_date: Optional[str] = None) -> pd.DataFrame:
        """获取 K 线并计算技术指标（warehouse 优先，baostock 兜底）

        Returns:
            含 ma/volume_ma/low_3m/year_low 等指标的 DataFrame
        """
        code = StockDataFetcher.normalize_code(code)
        if end_date is None:
            end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365 * DEFAULT_LOOKBACK_YEARS)).strftime("%Y-%m-%d")

        # 统一数据源（warehouse 优先、baostock 兜底，收敛散落 if-else —— FR-1.4）
        kline = self.delegate_fetch_kline(code, start_date, end_date)
        return TechnicalIndicators.compute_all(kline)

    def delegate_fetch_kline(self, code: str,
                             start_date: Optional[str] = None,
                             end_date: Optional[str] = None) -> pd.DataFrame:
        """把取数委托给正式 Published DataSource。"""
        ds = self.datasource or get_default_datasource()
        return ds.fetch_kline(code, start_date, end_date)

    def _fetch_from_warehouse(self, code_nodot: str,
                              start_date: str, end_date: str) -> pd.DataFrame:
        """从 warehouse 数据层读取 K 线（DuckDB 跨分区按 code+日期过滤，一次查询）。"""
        try:
            from pathlib import Path
            w = Warehouse()
            months = w.available_months("daily")
            if not months:
                return pd.DataFrame()
            files = [str(w.daily_partition(ym)) for ym in months]
            files = [f for f in files if Path(f).exists()]
            if not files:
                return pd.DataFrame()
            import duckdb
            con = duckdb.connect()
            try:
                file_list = "[" + ",".join("'" + f + "'" for f in files) + "]"
                kline = con.execute(
                    f"""SELECT date, open, high, low, close, volume, amount, turn,
                               pe_ttm, pb_mrq
                        FROM read_parquet({file_list})
                        WHERE code = '{code_nodot}'
                          AND date >= DATE '{start_date}'
                          AND date <= DATE '{end_date}'
                        ORDER BY date"""
                ).df()
            finally:
                con.close()
            if kline is None or kline.empty:
                return pd.DataFrame()
            return kline.drop_duplicates("date")
        except Exception as e:
            logger.warning("warehouse 读取 %s 失败(%s)，降级 baostock", code_nodot, e)
            return pd.DataFrame()

    def compute_dividend_anchor(self, code: str) -> Optional[float]:
        """计算股息锚；分红 Published 契约完成前正式路径返回不可用。"""
        code = StockDataFetcher.normalize_code(code)
        logger.info("股息 Published 数据集尚未就绪，跳过股息锚: %s", code)
        return None

    def fetch_context_data(self, code: str) -> tuple[pd.DataFrame, Optional[float]]:
        """一站式获取: (kline_with_indicators, dividend_anchor)"""
        kline = self.fetch_kline(code)
        anchor = self.compute_dividend_anchor(code)
        return kline, anchor
