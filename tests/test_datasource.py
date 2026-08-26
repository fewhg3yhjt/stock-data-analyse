"""FR-1.4 数据源抽象测试。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from StockInvestmentTool.datasource.base import (
    FallbackDataSource,
    KLINE_COLUMNS,
    OnlineSource,
    WarehouseSource,
)
from StockInvestmentTool.warehouse.storage import Warehouse


@pytest.fixture
def temp_warehouse(tmp_path):
    """临时仓库：构造一个月分区的确定性日线。"""
    w = Warehouse(base_dir=Path(tmp_path))
    dates = pd.bdate_range("2024-01-01", periods=100)
    df = pd.DataFrame({
        "date": dates, "code": "sh600900",
        "open": 20, "high": 21, "low": 19, "close": 20.5,
        "volume": 1000, "amount": 1e6, "peTTM": 11, "pbMRQ": 1.1, "turn": 0.5,
    })
    w.write_daily_partition("2024-01", df)
    w.write_daily_partition("2024-02", df)  # 覆盖两月分区
    return w


def test_warehouse_fetch_kline_fixed_columns(temp_warehouse):
    src = WarehouseSource(warehouse=temp_warehouse)
    df = src.fetch_kline("sh.600900", "2024-01-01", "2024-12-31")
    assert not df.empty
    # 只返回原始行情列（不含指标列）
    assert set(df.columns) == set(KLINE_COLUMNS)


def test_warehouse_fetch_daily_series_limit(temp_warehouse):
    src = WarehouseSource(warehouse=temp_warehouse)
    df = src.fetch_daily_series("sh.600900", days=30)
    assert len(df) <= 30


def test_warehouse_fetch_daily_series_order(temp_warehouse):
    src = WarehouseSource(warehouse=temp_warehouse)
    df = src.fetch_daily_series("sh.600900", days=100)
    assert df["date"].is_monotonic_increasing


def test_warehouse_empty_when_no_partition(tmp_path):
    w = Warehouse(base_dir=Path(tmp_path))
    src = WarehouseSource(warehouse=w)
    df = src.fetch_kline("sh.600000")
    assert df.empty


def test_fallback_tries_next_source(monkeypatch):
    """第一个源空，第二个源有值 → Fallback 返回第二个源。"""

    class EmptySource:
        def fetch_kline(self, code, start=None, end=None):
            return pd.DataFrame()

    class GoodSource:
        def fetch_kline(self, code, start=None, end=None):
            df = pd.DataFrame({"date": [pd.Timestamp("2024-01-01")],
                               "close": [10.0]})
            return df

    fb = FallbackDataSource([EmptySource(), GoodSource()])
    out = fb.fetch_kline("sh.600000")
    assert not out.empty


def test_online_normalizes_columns():
    """在线源包装 baostock 后，只保留固定列。"""

    class FakeFetcher:
        def get_kline(self, code, start=None, end=None):
            raw = pd.DataFrame({
                "date": [pd.Timestamp("2024-01-01")], "open": [1], "high": [2],
                "low": [0.5], "close": [1.5], "volume": [10], "amount": [1],
                "peTTM": [5], "pbMRQ": [0.5], "turn": [0.1], "extra_col": [9],
            })
            return raw

    src = OnlineSource.__new__(OnlineSource)
    src._fetcher = FakeFetcher()
    df = src.fetch_kline("sh.600000")
    assert "extra_col" not in df.columns
    assert set(df.columns) == set(KLINE_COLUMNS)


def test_warehouse_fetch_minute_series(tmp_path):
    from StockInvestmentTool.warehouse.minute import parse_tencent_minute

    warehouse = Warehouse(base_dir=Path(tmp_path))
    payload = {
        "data": {"sh600900": {"data": {
            "date": "20260826",
            "data": ["0930 28.20 100 1000", "0931 28.21 120 1210"],
        }}}
    }
    warehouse.minute_store().write(parse_tencent_minute(payload, "sh600900"))
    frame = WarehouseSource(warehouse=warehouse).fetch_minute_series(
        "sh.600900", "2026-08-26"
    )

    assert len(frame) == 2
    assert frame["close"].tolist() == [28.2, 28.21]
