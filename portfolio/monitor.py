"""价格监控 — 拉取最新行情 + 重算技术指标

提供刷新单个/全部持仓最新价的原始能力。策略判断委托给 PostPurchaseAdvisor。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from StockInvestmentTool.datasource.fetcher import StockDataFetcher
from StockInvestmentTool.datasource.indicators import TechnicalIndicators, ValuationHelper

logger = logging.getLogger(__name__)

# 分析窗口: 拉1年，确保 MA120/年内高点 等指标可用
DEFAULT_LOOKBACK_YEARS = 1


class PriceMonitor:
    """价格监控

    Args:
        fetcher: 数据获取器（可选，默认新建）
    """

    def __init__(self, fetcher: Optional[StockDataFetcher] = None):
        # 懒加载: 纯 DB 操作（list/show）不应触发 baostock 登录
        self._fetcher = fetcher

    @property
    def fetcher(self) -> StockDataFetcher:
        if self._fetcher is None:
            self._fetcher = StockDataFetcher()
        return self._fetcher

    def fetch_kline(self, code: str,
                    start_date: Optional[str] = None,
                    end_date: Optional[str] = None) -> pd.DataFrame:
        """拉取 K 线并计算技术指标

        Returns:
            含 ma/volume_ma/low_3m/year_low 等指标的 DataFrame
        """
        code = StockDataFetcher.normalize_code(code)
        if end_date is None:
            end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365 * DEFAULT_LOOKBACK_YEARS)).strftime("%Y-%m-%d")

        kline = self.fetcher.get_kline(code, start_date, end_date)
        return TechnicalIndicators.compute_all(kline)

    def compute_dividend_anchor(self, code: str) -> Optional[float]:
        """计算股息率极端低估锚（anchor_price_3）"""
        code = StockDataFetcher.normalize_code(code)
        try:
            end = datetime.now() - timedelta(days=1)
            py = end.year
            pq = ((end.month - 1) // 3) or 4
            if pq == 4:
                py -= 1
            divs = []
            for y in range(py - 5, py + 1):
                divs.extend(self.fetcher.get_dividend_data(code, y))
            if not divs:
                return None
            # 用最新收盘价作为当前价（粗算锚）
            kline = self.fetcher.get_kline(code)
            current_price = float(kline["close"].iloc[-1]) if len(kline) else 0
            anchor = ValuationHelper.triple_anchor(divs, current_price)
            if anchor and anchor.get("anchor_price_3"):
                return float(anchor["anchor_price_3"])
        except Exception as e:
            logger.warning("股息率锚计算失败 %s: %s", code, e)
        return None

    def fetch_context_data(self, code: str) -> tuple[pd.DataFrame, Optional[float]]:
        """一站式获取: (kline_with_indicators, dividend_anchor)"""
        kline = self.fetch_kline(code)
        anchor = self.compute_dividend_anchor(code)
        return kline, anchor
