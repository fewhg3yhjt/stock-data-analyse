from __future__ import annotations

import pandas as pd

from StockInvestmentTool.portfolio.dashboard import DashboardService
from StockInvestmentTool.warehouse.minute import MinuteStore
from StockInvestmentTool.warehouse.storage import Warehouse


def test_local_snapshots_prefer_minute_without_network(tmp_path, monkeypatch):
    warehouse = Warehouse(tmp_path / "warehouse")
    frame = pd.DataFrame({"code": ["sh600900"], "trade_date": ["2026-08-27"],
                          "time": ["2026-08-27 14:59:00"], "close": [28.5]})
    MinuteStore(tmp_path / "warehouse").write(frame)
    monkeypatch.setattr("StockInvestmentTool.config.Config.DATA_DIR", tmp_path)
    service = DashboardService(object())
    rows = [{"code": "sh.600900", "price": None}]
    result = service._apply_local_snapshots(rows)
    assert result[0]["price"] == 28.5
    assert result[0]["data_source"] == "minute_snapshot"
