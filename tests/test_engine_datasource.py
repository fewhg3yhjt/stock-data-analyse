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


def test_analysis_engine_prefers_warehouse_fundamentals(monkeypatch, kline, tmp_path):
    from StockInvestmentTool.core.engine import AnalysisEngine
    from StockInvestmentTool.datasource.base import WarehouseSource

    class FakeWarehouse:
        def get_instrument(self, code): return {"code": code, "name": "测试"}
        def read_fundamentals(self, code):
            return pd.DataFrame([{"stat_date": "2025-12-31", "roe": 12.0}])

    monkeypatch.setattr(WarehouseSource, "__init__", lambda self, warehouse=None: setattr(self, "_warehouse", FakeWarehouse()))
    class FakeFetcher:
        def get_profit_data(self, *args): raise AssertionError("不应读取在线盈利")
        def get_dividend_data(self, *args): return []
    result = AnalysisEngine(data_source=type("S", (), {"fetch_kline": lambda *_: kline})())._fetch(FakeFetcher(), "sh600900", "2025-01-01", "2025-12-31")
    assert result["data_sources"]["fundamentals"] == "warehouse"
    assert result["profit"]["roe"] == 12.0
