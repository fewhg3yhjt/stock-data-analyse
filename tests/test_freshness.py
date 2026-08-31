"""Freshness service tests use an isolated temporary warehouse."""

from datetime import datetime

import pandas as pd

from StockInvestmentTool.ops.freshness import classify_freshness, data_status
from StockInvestmentTool.warehouse.storage import Warehouse


def test_classify_trade_day_lag():
    expected = datetime(2026, 8, 27).date()
    assert classify_freshness("2026-08-27", expected) == "healthy"
    assert classify_freshness("2026-08-26", expected) == "stale"
    assert classify_freshness("2026-08-24", expected) == "critical"
    assert classify_freshness(None, expected) == "empty"


def test_daily_expected_date_is_last_completed_trade_day():
    from StockInvestmentTool.ops.freshness import latest_completed_trade_day
    # 交易日 15:35 之后视为当天已收盘
    assert latest_completed_trade_day(datetime(2026, 8, 27, 21)) == datetime(2026, 8, 27).date()
    # 交易日盘前（早于 15:35）返回上一交易日
    assert latest_completed_trade_day(datetime(2026, 8, 27, 10)) == datetime(2026, 8, 26).date()


def test_data_status_reads_actual_files_and_job_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("WAREHOUSE_DAILY_SYNC", "1")
    warehouse = Warehouse(tmp_path / "warehouse")
    frame = pd.DataFrame({"date": ["2026-08-26"], "code": ["sh600900"], "close": [10.0]})
    warehouse.write_daily_partition("2026-08", frame)
    warehouse.write_indicator_partition("2026-08", frame)
    result = data_status(
        warehouse=warehouse,
        job_runs=[{"job_name": "daily_tasks", "status": "failed", "started_at": "2026-08-27T15:00:00", "finished_at": "2026-08-27T15:01:00", "error": "provider timeout"}],
        now=datetime(2026, 8, 27, 16),
    )
    datasets = {item["dataset"]: item for item in result["datasets"]}
    assert datasets["daily"]["status"] == "failed"
    assert datasets["daily"]["last_error"] == "provider timeout"
    assert datasets["indicators"]["latest_value"] == "2026-08-26"
    assert result["expected_trade_day"] == "2026-08-27"
