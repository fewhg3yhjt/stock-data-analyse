from datetime import datetime

import pandas as pd

from StockInvestmentTool.ops.freshness import quick_daily_status
from StockInvestmentTool.warehouse.storage import Warehouse


def test_quick_daily_status_for_workflow_gate(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.write_daily_partition("2026-08", pd.DataFrame({
        "date": ["2026-08-21"], "code": ["sh600900"], "close": [10],
    }))
    status = quick_daily_status(warehouse=warehouse, now=datetime(2026, 8, 27, 16))
    assert status["latest_value"] == "2026-08-21"
    assert status["status"] == "critical"
