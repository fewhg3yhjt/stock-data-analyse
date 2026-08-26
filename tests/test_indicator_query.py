"""Indicator partition query only projects one instrument."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from StockInvestmentTool.warehouse.storage import Warehouse


def test_read_indicator_code_filters_code(tmp_path):
    warehouse = Warehouse(base_dir=Path(tmp_path))
    frame = pd.DataFrame({
        "date": pd.to_datetime(["2026-08-01", "2026-08-02", "2026-08-01"]),
        "code": ["sh600900", "sh600900", "sz000001"],
        "MA20": [1.0, 2.0, 3.0],
    })
    warehouse.write_indicator_partition("2026-08", frame)

    result = warehouse.read_indicator_code("sh.600900", days=10)

    assert result["code"].unique().tolist() == ["sh600900"]
    assert len(result) == 2
