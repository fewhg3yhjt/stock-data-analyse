"""Warehouse benchmark returns measurable, structured results."""

from __future__ import annotations

import pandas as pd

from StockInvestmentTool.datasource.base import WarehouseSource


def test_benchmark_reports_rows_and_latency(monkeypatch, tmp_path):
    from scripts import benchmark_data

    frame = pd.DataFrame({"date": pd.to_datetime(["2026-08-26"]), "close": [10.0]})
    monkeypatch.setattr(WarehouseSource, "fetch_daily_series", lambda *_args, **_kwargs: frame)
    result = benchmark_data.benchmark("sh600900", days=10, repeat=2)

    assert result["rows"] == 1
    assert result["repeat"] == 2
    assert result["min_ms"] >= 0
    assert result["avg_ms"] >= 0
