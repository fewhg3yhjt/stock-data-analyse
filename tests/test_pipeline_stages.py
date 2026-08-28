import json
import hashlib
import sqlite3

import pandas as pd

from StockInvestmentTool.warehouse.daily_build import DailyBuilder
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.quality import check_stock_daily
from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.storage import Warehouse
from scripts.convert_legacy_daily import convert


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


def test_publish_can_rollback_to_previous_version(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    state = PipelineState(warehouse.meta_db_path)
    versions = []
    for close in (10.2, 10.8):
        path = warehouse.base_dir / f"candidate_{close}.parquet"
        pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
                      "open": [10.0], "high": [11.0], "low": [9.0], "close": [close],
                      "volume": [100.0], "amount": [1000.0]}).to_parquet(path, index=False)
        build = {"version_id": f"v-{close}", "partition": "2026-08", "path": path,
                 "row_count": 1, "symbol_count": 1, "checksum": hashlib.sha256(path.read_bytes()).hexdigest()}
        version = state.create_version(build, source_batches=[])
        state.quality(version, status="PASS", checks={}, publish_allowed=True)
        Publisher(warehouse).publish(version)
        versions.append(version)
    assert warehouse.read_daily("2026-08").iloc[0]["close"] == 10.8
    Publisher(warehouse).rollback("stock_daily", "2026-08")
    assert warehouse.read_daily("2026-08").iloc[0]["close"] == 10.2
    assert state.current("stock_daily", "2026-08")[0] == versions[0]


def test_legacy_conversion_isolated_and_repeatable(tmp_path):
    source = tmp_path / "daily"
    source.mkdir()
    old = source / "2026-08.parquet"
    pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"], "close": [10.2]}).to_parquet(old, index=False)
    before = old.read_bytes()
    report = convert(source, tmp_path / "migration")
    assert len(report["records"]) == 1
    assert report["records"][0]["source_name"] == "legacy_daily"
    assert old.read_bytes() == before
    second = convert(source, tmp_path / "migration")
    assert second["records"][0]["output_path"] == report["records"][0]["output_path"]


def test_builder_auto_selects_batches_and_same_content_is_idempotent(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily()
    source = capture_frames(
        warehouse, dataset_name="stock_daily", source_name="tencent",
        run_date="2026-08-28", trade_date_start="2026-08-28", trade_date_end="2026-08-28",
        frames=[pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
                              "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
                              "volume": [1.0], "amount": [0.1]})], expected_symbols=1,
        success_symbols=1, universe_id="u", request_context={"fixture": True})
    builder = DailyBuilder(warehouse)
    assert builder.select_raw_batches("2026-08") == [("tencent", source["raw"]["path"], source["batch_id"])]
    first = builder.build_partition("2026-08", include_current=False)
    second = builder.build_partition("2026-08", include_current=False)
    assert first["version_id"] == second["version_id"]
    assert first["checksum"] == second["checksum"]
    assert first["source_batches"] == [source["batch_id"]]


def test_quality_reports_freshness_and_source_conflicts(tmp_path):
    path = tmp_path / "candidate.parquet"
    frame = pd.DataFrame({"date": [pd.Timestamp("2026-08-27")] * 1000,
                          "code": [f"sh{i:06d}" for i in range(1000)],
                          "open": [10.0] * 1000, "high": [10.5] * 1000,
                          "low": [9.8] * 1000, "close": [10.2] * 1000,
                          "volume": [100.0] * 1000, "amount": [1000.0] * 1000})
    frame.to_parquet(path, index=False)
    result = check_stock_daily(path, expected_symbols=1, expected_trade_date="2026-08-28",
                               source_conflicts=[{"code": "sh600000", "field": "close"}])
    assert result["status"] == "WARNING"
    assert result["checks"]["freshness"]["status"] == "WARNING"
    assert result["checks"]["source_conflict"]["count"] == 1


def test_builder_selects_latest_batch_and_reports_legacy_diff(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily()
    warehouse.write_daily_partition("2026-08", pd.DataFrame({
        "date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"], "close": [10.0],
    }))
    for close in (10.1, 10.2):
        capture_frames(
            warehouse, dataset_name="stock_daily", source_name="tencent",
            run_date="2026-08-28", trade_date_start="2026-08-28", trade_date_end="2026-08-28",
            frames=[pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
                                  "open": [10.0], "high": [10.5], "low": [9.8], "close": [close],
                                  "volume": [1.0], "amount": [0.1]})], expected_symbols=1,
            success_symbols=1, universe_id="u", request_context={"close": close},
        )
    build = DailyBuilder(warehouse).build_partition("2026-08", include_current=True)
    assert build["row_count"] == 1
    assert build["diff_report"]["current_rows"] == 1
    assert build["diff_report"]["field_differences"] == 1


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
