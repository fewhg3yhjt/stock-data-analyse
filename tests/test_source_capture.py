import pandas as pd

from StockInvestmentTool.warehouse.metadata import MetadataStore
from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.source_batches import SourceBatchStore
from StockInvestmentTool.warehouse.storage import Warehouse
from StockInvestmentTool.warehouse.dataset_config import load_dataset_config


def test_all_low_frequency_sources_share_batch_capture_contract(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    frames = {
        "industry": pd.DataFrame({"code": ["sh600000"], "industry": ["银行"]}),
        "fundamentals": pd.DataFrame({"code": ["sh600000"], "stat_date": [pd.Timestamp("2026-06-30")], "roe": [8.0]}),
        "valuation_daily": pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"], "peTTM": [6.0]}),
        "money_flow_daily": pd.DataFrame({"period": ["now"], "code": ["sh600000"], "net": [1.0]}),
    }
    metadata = MetadataStore(warehouse.meta_db_path)
    for name in ("industry", "fundamentals", "valuation_daily", "money_flow_daily"):
        metadata.register_dataset(name)
        result = capture_frames(
            warehouse, dataset_name=name, source_name="fixture", frames=[frames[name]],
            expected_symbols=1, success_symbols=1, universe_id=f"u_{name}",
            request_context={"fixture": True},
        )
        row = SourceBatchStore(warehouse.meta_db_path).get(result["batch_id"])
        assert row["dataset_name"] == name
        assert row["status"] == "success"
        assert row["row_count"] == 1
        assert row["raw_path"].endswith(".parquet")


def test_all_auxiliary_dataset_definitions_are_loadable():
    for name in ("industry", "fundamentals", "valuation_daily", "money_flow_daily"):
        config = load_dataset_config(name)
        assert config["dataset"]["name"] == name
        assert config["_config_checksum"]
