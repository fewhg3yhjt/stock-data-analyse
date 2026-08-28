import pandas as pd

from StockInvestmentTool.warehouse.storage import Warehouse


def test_stock_daily_metadata_registration_and_partition_index_are_idempotent(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    daily = warehouse.daily_dir / "2026-08.parquet"
    pd.DataFrame({
        "date": pd.to_datetime(["2026-08-27", "2026-08-28"]),
        "code": ["sh600000", "sz000001"],
        "close": [10.0, 12.0],
    }).to_parquet(daily, index=False)

    metadata = warehouse.metadata
    metadata.register_stock_daily()
    assert metadata.index_daily_partitions(warehouse.daily_dir) == 1
    metadata.register_stock_daily()
    assert metadata.index_daily_partitions(warehouse.daily_dir) == 1

    with warehouse._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM dataset_registry").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM dataset_fields WHERE dataset_name='stock_daily'").fetchone()[0] == 11
        assert conn.execute("SELECT COUNT(*) FROM dataset_sources WHERE dataset_name='stock_daily'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM dataset_consumers WHERE dataset_name='stock_daily'").fetchone()[0] == 5
    partition = metadata.list_partitions()[0]
    assert partition["partition_key"] == "2026-08"
    assert partition["row_count"] == 2
    assert partition["status"] == "legacy"
