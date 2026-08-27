from __future__ import annotations

import pandas as pd

from StockInvestmentTool.market_discovery.service import discover_stocks, stock_series
from StockInvestmentTool.market_discovery.storage import DiscoveryRunStore
from StockInvestmentTool.warehouse.storage import Warehouse


def _daily_frame():
    dates = pd.date_range("2026-01-01", periods=90, freq="B")
    rows = []
    for code, closes, volumes in (
        ("sh600900", list(range(10, 100)), list(range(1000, 910, -1)),),
        ("sz000001", list(range(100, 10, -1)), list(range(1000, 1090)),),
    ):
        for day, close, volume in zip(dates, closes, volumes):
            rows.append({"date": day, "code": code, "open": close,
                         "high": close + 1, "low": close - 1,
                         "close": close, "volume": volume, "amount": close * volume})
    return pd.DataFrame(rows)


def test_discover_stocks_returns_explainable_volume_signal(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    frame = _daily_frame()
    warehouse.write_daily_partition("2026-04", frame)
    result = discover_stocks({"lookback_days": 3, "min_up_days": 2,
                              "signal": "price_up_volume_down"},
                             warehouse=warehouse, top_n=10)
    assert result["count"] == 1
    assert result["total_count"] == 1
    assert result["items"][0]["code"] == "sh600900"
    assert "上涨缩量" in result["items"][0]["signal_tags"]
    assert result["items"][0]["name"] == "sh600900"


def test_stock_series_uses_local_daily_data(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.write_daily_partition("2026-04", _daily_frame())
    result = stock_series("sh600900", warehouse=warehouse, days=20)
    assert len(result["dates"]) == 20
    assert result["close"][-1] == 99


def test_discovery_run_store_records_conditions(tmp_path):
    store = DiscoveryRunStore(tmp_path / "runs.db")
    run_id = store.save(as_of="2026-05-06", conditions={"signal": "volume_spike"}, result_count=2)
    assert store.recent(1)[0]["id"] == run_id
    assert store.recent(1)[0]["conditions"]["signal"] == "volume_spike"
