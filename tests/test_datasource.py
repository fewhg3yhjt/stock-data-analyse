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
        "volume": 1000, "amount": 1e6, "pe_ttm": 11, "pb_mrq": 1.1, "turn": 0.5,
    })
    w.write_daily_partition("2024-01", df)
    w.write_daily_partition("2024-02", df)  # 覆盖两月分区
    return w


def test_warehouse_fetch_kline_fixed_columns(temp_warehouse):
    src = WarehouseSource(warehouse=temp_warehouse, allow_legacy=True)
    df = src.fetch_kline("sh.600900", "2024-01-01", "2024-12-31")
    assert not df.empty
    # 只返回原始行情列（不含指标列）
    assert set(df.columns) == set(KLINE_COLUMNS)


def test_warehouse_fetch_daily_series_limit(temp_warehouse):
    src = WarehouseSource(warehouse=temp_warehouse, allow_legacy=True)
    df = src.fetch_daily_series("sh.600900", days=30)
    assert len(df) <= 30


def test_warehouse_fetch_daily_series_order(temp_warehouse):
    src = WarehouseSource(warehouse=temp_warehouse, allow_legacy=True)
    df = src.fetch_daily_series("sh.600900", days=100)
    assert df["date"].is_monotonic_increasing


def test_warehouse_empty_when_no_partition(tmp_path):
    w = Warehouse(base_dir=Path(tmp_path))
    src = WarehouseSource(warehouse=w, allow_legacy=True)
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


@pytest.mark.parametrize("code", ["../../etc/passwd", "sh60090x", "unknown"])
def test_warehouse_rejects_invalid_code(temp_warehouse, code):
    with pytest.raises(ValueError, match="股票代码格式无效"):
        WarehouseSource(warehouse=temp_warehouse).fetch_daily_series(code)


def test_warehouse_rejects_invalid_range(temp_warehouse):
    with pytest.raises(ValueError, match="开始日期不能晚于结束日期"):
        WarehouseSource(warehouse=temp_warehouse).fetch_kline(
            "sh600900", "2025-01-01", "2024-01-01"
        )


def _publish_stock_daily(warehouse) -> str:
    from StockInvestmentTool.warehouse.pipeline_state import PipelineState
    from StockInvestmentTool.warehouse.publish import Publisher
    from StockInvestmentTool.warehouse.quality import check_stock_daily
    from StockInvestmentTool.warehouse.source_capture import capture_frames
    from StockInvestmentTool.warehouse.daily_build import DailyBuilder

    warehouse.metadata.register_stock_daily()
    frame = pd.DataFrame({
        "date": pd.to_datetime(["2026-08-03", "2026-08-04"]), "code": ["sh600900", "sh600900"],
        "open": [20.0, 20.5], "high": [21.0, 21.2], "low": [19.8, 20.0], "close": [20.5, 20.8],
        # Tencent Raw transport units: hand / wan yuan, chosen to yield a
        # plausible amount / (share volume * close) near 1 after conversion.
        "volume": [10, 11], "amount": [2.05, 2.288], "turn": [0.5, 0.6],
        "pe_ttm": [11, 11], "pb_mrq": [1.1, 1.1],
    })
    source = capture_frames(
        warehouse, dataset_name="stock_daily", source_name="tencent",
        frames=[frame], expected_symbols=1, success_symbols=1,
        universe_id="u", request_context={"fixture": True},
    )
    build = DailyBuilder(warehouse).build_partition("2026-08", [("tencent", source["raw"]["path"])], include_current=False)
    state = PipelineState(warehouse.meta_db_path)
    version = state.create_version(build, source_batches=[source["batch_id"]])
    quality = check_stock_daily(build["path"], expected_symbols=1)
    state.quality(version, status=quality["status"], checks=quality["checks"],
                  publish_allowed=quality["publish_allowed"])
    Publisher(warehouse).publish(version)
    return version


def test_warehouse_source_reads_published_dataset(tmp_path):
    """有治理版本时，WarehouseSource 走 Published Dataset（版本/质量/checksum 治理）。"""
    w = Warehouse(base_dir=Path(tmp_path))
    _publish_stock_daily(w)
    src = WarehouseSource(warehouse=w)
    df = src.fetch_kline("sh.600900", "2026-08-01", "2026-08-31")
    assert not df.empty
    assert set(df.columns) == set(KLINE_COLUMNS)
    assert df["close"].tolist() == [20.5, 20.8]
    # 治理路径应返回发布时间范围的数据，而非直读分区
    assert len(df) == 2


def test_warehouse_source_requires_published_dataset(tmp_path):
    warehouse = Warehouse(base_dir=Path(tmp_path) / "warehouse",
                          meta_db_path=Path(tmp_path) / "management.db")
    with pytest.raises(Exception, match="Published Dataset"):
        WarehouseSource(warehouse=warehouse).fetch_kline(
            "sh600900", "2026-08-01", "2026-08-31"
        )
