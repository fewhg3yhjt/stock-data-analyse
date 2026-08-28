from pathlib import Path

import pandas as pd

from StockInvestmentTool.warehouse.baseline import audit_stock_daily


def test_audit_stock_daily_is_read_only_and_reports_partitions(tmp_path):
    daily = tmp_path / "daily"
    daily.mkdir()
    frame = pd.DataFrame({
        "date": pd.to_datetime(["2026-08-27", "2026-08-27", "2026-08-28"]),
        "code": ["sh600000", "sh600000", "sz000001"],
        "close": [10.0, 10.0, 12.0],
    })
    path = daily / "2026-08.parquet"
    frame.to_parquet(path, index=False)
    before = path.read_bytes()

    report = audit_stock_daily(tmp_path)

    assert report["partition_count"] == 1
    assert report["row_count"] == 3
    assert report["symbol_count"] == 2
    assert report["duplicate_primary_keys"] == 1
    assert report["partitions"][0]["min_date"] == "2026-08-27"
    assert report["partitions"][0]["max_date"] == "2026-08-28"
    assert path.read_bytes() == before


def test_audit_can_select_small_batch_of_months(tmp_path):
    daily = tmp_path / "daily"
    daily.mkdir()
    for month in ("2026-07", "2026-08"):
        pd.DataFrame({"date": [pd.Timestamp(f"{month}-01")], "code": ["sh600000"]}).to_parquet(
            daily / f"{month}.parquet", index=False
        )

    report = audit_stock_daily(tmp_path, ["2026-08"])

    assert [item["partition"] for item in report["partitions"]] == ["2026-08"]
