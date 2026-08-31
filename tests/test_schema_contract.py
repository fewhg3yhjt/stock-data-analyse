# -*- coding: utf-8 -*-
"""阶段七：统一字段与 Schema 契约。

验证 WarehouseSource 只查真实字段、可选字段缺失补空列、
必填字段缺失抛明确契约错误（不吞异常伪装空表）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from StockInvestmentTool.datasource.base import KLINE_COLUMNS, WarehouseSource
from StockInvestmentTool.warehouse.storage import Warehouse


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(base_dir=Path(tmp_path))
    dates = pd.bdate_range("2026-08-01", periods=60)
    w.write_daily_partition("2026-08", pd.DataFrame({
        "date": dates, "code": "sh600900",
        "open": 20, "high": 21, "low": 19, "close": 20.5,
        "volume": 1000, "amount": 1e6,
    }))
    return w


def test_missing_optional_columns_are_backfilled(warehouse):
    """daily 无 pe_ttm/pb_mrq/turn 时，查询返回空列而非失败。"""
    src = WarehouseSource(warehouse=warehouse)
    df = src.fetch_kline("sh.600900", "2026-08-01", "2026-08-31")
    assert not df.empty
    assert set(df.columns) == set(KLINE_COLUMNS)
    assert df["pe_ttm"].isna().all()
    assert df["pb_mrq"].isna().all()
    assert df["turn"].isna().all()


def test_missing_required_columns_raise_contract_error(tmp_path):
    """daily 缺必填字段（如 close）时抛明确契约错误，不返回空表。"""
    w = Warehouse(base_dir=Path(tmp_path))
    w.write_daily_partition("2026-08", pd.DataFrame({
        "date": pd.bdate_range("2026-08-01", periods=5), "code": "sh600900",
        "open": [1.0] * 5,
    }))
    src = WarehouseSource(warehouse=w)
    with pytest.raises(ValueError, match="缺少必填字段"):
        src.fetch_kline("sh.600900", "2026-08-01", "2026-08-31")


def test_daily_series_missing_optional_backfilled(warehouse):
    src = WarehouseSource(warehouse=warehouse)
    df = src.fetch_daily_series("sh.600900", days=30)
    assert not df.empty
    assert set(df.columns) == set(KLINE_COLUMNS)