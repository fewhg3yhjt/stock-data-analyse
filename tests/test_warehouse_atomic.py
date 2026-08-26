"""Warehouse parquet writes replace final files atomically."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from StockInvestmentTool.warehouse.storage import Warehouse


def test_daily_write_leaves_no_temp_files(tmp_path):
    warehouse = Warehouse(base_dir=Path(tmp_path))
    frame = pd.DataFrame({"date": [pd.Timestamp("2026-08-26")], "code": ["sh600900"], "close": [10.0]})

    warehouse.write_daily_partition("2026-08", frame)

    assert warehouse.daily_partition("2026-08").exists()
    assert not list(warehouse.daily_dir.glob("*.tmp"))
