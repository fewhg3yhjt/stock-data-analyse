import pandas as pd

from scripts.audit_data_migration import audit_warehouse


def test_audit_classifies_clean_and_invalid_files(tmp_path):
    warehouse = tmp_path / "warehouse"
    (warehouse / "daily").mkdir(parents=True)
    pd.DataFrame({"date": pd.to_datetime(["2026-01-02"]), "code": ["sh600000"],
                  "close": [10.0]}).to_parquet(warehouse / "daily" / "2026-01.parquet")
    report = audit_warehouse(warehouse)
    record = report["datasets"]["stock_daily"]["records"][0]
    assert record["readable"] is True
    assert record["duplicate_primary_keys"] == 0
    assert record["migration_class"] == "adapter_rebuild"
    assert report["read_only"] is True
