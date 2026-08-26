"""AnalysisEngine K-line retrieval is injectable through DataSource."""

from __future__ import annotations

import pandas as pd


def test_analysis_engine_uses_injected_data_source(kline):
    from StockInvestmentTool.core.engine import AnalysisEngine

    class FakeSource:
        def fetch_kline(self, code, start=None, end=None):
            return kline.copy()

    class FakeFetcher:
        def get_stock_basic(self, code):
            return {}

        def get_profit_data(self, code, year, quarter):
            return {}

        def get_dividend_data(self, code, year):
            return []

    engine = AnalysisEngine(data_source=FakeSource())
    result = engine._fetch(FakeFetcher(), "sh600900", "2024-01-01", "2024-06-30")

    assert len(result["kline"]) == len(kline)
