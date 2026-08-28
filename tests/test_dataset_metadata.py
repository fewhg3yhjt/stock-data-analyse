import pandas as pd
import pytest
import yaml

from StockInvestmentTool.warehouse.dataset_config import DatasetConfigError, load_dataset_config, validate_dataset_config
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

    with warehouse._conn() as conn:
        row = conn.execute("SELECT config_path, config_checksum FROM dataset_registry").fetchone()
    assert row[0].endswith("config/datasets/stock_daily.yaml")
    assert len(row[1]) == 64


def test_dataset_config_rejects_invalid_primary_key_definition():
    config = load_dataset_config()
    config["dataset"]["primary_keys"] = ["missing"]
    with pytest.raises(DatasetConfigError):
        validate_dataset_config(config)


def test_dataset_config_missing_file_is_explicit_error(tmp_path):
    with pytest.raises(DatasetConfigError, match="配置不存在"):
        load_dataset_config(path=tmp_path / "missing.yaml")


def test_metadata_projection_follows_yaml_changes(tmp_path):
    config = load_dataset_config()
    config_path = tmp_path / "stock_daily.yaml"
    config["dataset"]["description"] = "测试版日线定义"
    config["sources"][0]["role"] = "validate"
    config["consumers"] = config["consumers"][:1]
    config.pop("_config_path", None)
    config.pop("_config_checksum", None)
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")

    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily(config_path)

    with warehouse._conn() as conn:
        description = conn.execute("SELECT description FROM dataset_registry").fetchone()[0]
        role = conn.execute("SELECT role FROM dataset_sources WHERE source_name='tencent'").fetchone()[0]
        source_count = conn.execute("SELECT COUNT(*) FROM dataset_sources").fetchone()[0]
        consumer_count = conn.execute("SELECT COUNT(*) FROM dataset_consumers").fetchone()[0]
    assert description == "测试版日线定义"
    assert role == "validate"
    assert source_count == 2
    assert consumer_count == 1
