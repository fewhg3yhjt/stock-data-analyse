import json
import sqlite3

import pandas as pd

from StockInvestmentTool.warehouse.daily_build import DailyBuilder
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.quality import check_stock_daily
from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.storage import Warehouse


def test_validation_data_runs_candidate_quality_and_publish(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily()
    source = capture_frames(
        warehouse, dataset_name="stock_daily", source_name="tencent",
        frames=[pd.DataFrame({
            "date": pd.to_datetime(["2026-08-28"]), "code": ["sh600000"],
            "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
            "volume": [1.0], "amount": [0.1], "turn": [1.0],
        })], expected_symbols=1, success_symbols=1, universe_id="u", request_context={"fixture": True}
    )
    build = DailyBuilder(warehouse).build_partition("2026-08", [("tencent", source["raw"]["path"])], include_current=False)
    state = PipelineState(warehouse.meta_db_path)
    version = state.create_version(build, source_batches=[source["batch_id"]])
    quality = check_stock_daily(build["path"], expected_symbols=1)
    state.quality(version, status=quality["status"], checks=quality["checks"], publish_allowed=quality["publish_allowed"])
    result = Publisher(warehouse).publish(version)

    assert result["version_id"] == version
    assert state.current("stock_daily", "2026-08")[0] == version
    assert warehouse.read_daily("2026-08")["close"].tolist() == [10.2]


def test_failed_quality_blocks_publish_without_formal_file(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily()
    candidate = warehouse.base_dir / "candidate.parquet"
    pd.DataFrame({
        "date": pd.to_datetime(["2026-08-28", "2026-08-28"]),
        "code": ["sh600000", "sh600000"], "open": [10.0, 10.0],
        "high": [10.5, 10.5], "low": [9.8, 9.8], "close": [10.2, 10.2],
        "volume": [100.0, 100.0], "amount": [1000.0, 1000.0],
    }).to_parquet(candidate, index=False)
    build = {"version_id": "bad-v1", "partition": "2026-08", "path": candidate,
             "row_count": 2, "symbol_count": 1, "checksum": "bad"}
    state = PipelineState(warehouse.meta_db_path)
    version = state.create_version(build, source_batches=[])
    quality = check_stock_daily(candidate, expected_symbols=1)
    state.quality(version, status=quality["status"], checks=quality["checks"], publish_allowed=quality["publish_allowed"])

    try:
        Publisher(warehouse).publish(version)
    except ValueError:
        pass
    else:
        raise AssertionError("FAIL candidate should not publish")
    assert not warehouse.daily_partition("2026-08").exists()


def test_builder_applies_yaml_units_once_and_tencent_wins(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily()
    tencent = capture_frames(
        warehouse, dataset_name="stock_daily", source_name="tencent",
        frames=[pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
                              "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
                              "volume": [1.0], "amount": [0.1], "turn": [1.0]})],
        expected_symbols=1, success_symbols=1, universe_id="u", request_context={"fixture": True}
    )
    baostock = capture_frames(
        warehouse, dataset_name="stock_daily", source_name="baostock",
        frames=[pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
                              "open": [11.0], "high": [11.5], "low": [10.8], "close": [11.2],
                              "volume": [999.0], "amount": [999.0], "turn": [2.0]})],
        expected_symbols=1, success_symbols=1, universe_id="u", request_context={"fixture": True}
    )

    build = DailyBuilder(warehouse).build_partition(
        "2026-08", [("baostock", baostock["raw"]["path"]), ("tencent", tencent["raw"]["path"])],
        include_current=False,
    )
    result = pd.read_parquet(build["path"])
    assert result.iloc[0]["close"] == 10.2
    assert result.iloc[0]["volume"] == 100.0
    assert result.iloc[0]["amount"] == 1000.0
