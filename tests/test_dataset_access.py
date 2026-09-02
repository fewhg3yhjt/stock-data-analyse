import pandas as pd
import pytest

from StockInvestmentTool.warehouse.daily_build import DailyBuilder
from StockInvestmentTool.warehouse.datasets import DatasetAccessError, load_dataset
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.quality import check_stock_daily
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.storage import Warehouse
from StockInvestmentTool.warehouse.indicators_build import IndicatorsBuilder


def test_access_reads_current_only_and_returns_context(tmp_path, monkeypatch):
    monkeypatch.delenv("MANAGEMENT_DB_PATH", raising=False)
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily()
    source = capture_frames(
        warehouse, dataset_name="stock_daily", source_name="tencent",
        frames=[pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
                              "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
                              "volume": [1.0], "amount": [0.1], "turn": [1.0]})],
        expected_symbols=1, success_symbols=1, universe_id="u", request_context={"fixture": True})
    build = DailyBuilder(warehouse).build_partition("2026-08", [("tencent", source["raw"]["path"])], include_current=False)
    state = PipelineState(warehouse.meta_db_path)
    version = state.create_version(build, source_batches=[source["batch_id"]])
    quality = check_stock_daily(build["path"], expected_symbols=1)
    state.quality(version, status=quality["status"], checks=quality["checks"], publish_allowed=True)
    Publisher(warehouse).publish(version)
    result = load_dataset(warehouse, "stock_daily", "2026-08-28", "2026-08-28")
    assert result.data["code"].tolist() == ["sh600000"]
    assert result.context["partition_versions"]["2026-08"] == version
    assert result.context["dataset_refs"]["stock_daily"]["partition_versions"]["2026-08"] == version
    assert result.context["source"] == "published_dataset"
    assert result.context["data_as_of"] == result.context["returned_end"]
    assert result.context["fallback_used"] is False


def test_access_rejects_candidate_without_current(tmp_path, monkeypatch):
    monkeypatch.delenv("MANAGEMENT_DB_PATH", raising=False)
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily()
    candidate = warehouse.base_dir / "candidate.parquet"
    pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"]}).to_parquet(candidate, index=False)
    with pytest.raises(DatasetAccessError):
        load_dataset(warehouse, "stock_daily", "2026-08-28", "2026-08-28")


def test_access_legacy_mode_is_explicit(tmp_path, monkeypatch):
    monkeypatch.delenv("MANAGEMENT_DB_PATH", raising=False)
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily()
    warehouse.write_daily_partition("2026-08", pd.DataFrame({
        "date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"], "close": [10.2],
    }))
    result = load_dataset(warehouse, "stock_daily", "2026-08-28", "2026-08-28", allow_legacy=True)
    assert result.context["fallback_used"] is True
    assert result.context["quality_status"] == "LEGACY"


def test_access_falls_back_to_local_partition_without_current_index(tmp_path, monkeypatch):
    monkeypatch.delenv("MANAGEMENT_DB_PATH", raising=False)
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily()
    warehouse.write_daily_partition("2026-08", pd.DataFrame({
        "date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
        "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
        "volume": [1000.0], "amount": [100000.0],
    }))

    result = load_dataset(warehouse, "stock_daily", "2026-08-28", "2026-08-28")

    assert result.data["code"].tolist() == ["sh600000"]
    assert result.context["source"] == "local_partition_fallback"
    assert result.context["fallback_used"] is True
    assert result.context["quality_status"] == "WARNING"


def test_access_falls_back_for_one_missing_month_when_other_month_is_published(tmp_path, monkeypatch):
    monkeypatch.delenv("MANAGEMENT_DB_PATH", raising=False)
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily()
    warehouse.write_daily_partition("2026-08", pd.DataFrame({
        "date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
        "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
        "volume": [1000.0], "amount": [100000.0],
    }))
    source = capture_frames(
        warehouse, dataset_name="stock_daily", source_name="tencent",
        frames=[pd.DataFrame({
            "date": [pd.Timestamp("2026-09-01")], "code": ["sh600000"],
            "open": [10.1], "high": [10.6], "low": [10.0], "close": [10.4],
            "volume": [1000.0], "amount": [100000.0], "turn": [1.0],
        })], expected_symbols=1, success_symbols=1, universe_id="u",
        request_context={"fixture": True})
    build = DailyBuilder(warehouse).build_partition(
        "2026-09", [("tencent", source["raw"]["path"])], include_current=False)
    state = PipelineState(warehouse.meta_db_path)
    version = state.create_version(build, source_batches=[source["batch_id"]])
    quality = check_stock_daily(build["path"], expected_symbols=1)
    state.quality(version, status=quality["status"], checks=quality["checks"], publish_allowed=True)
    Publisher(warehouse).publish(version)

    result = load_dataset(warehouse, "stock_daily", "2026-08-28", "2026-09-01")

    assert result.data["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-08-28", "2026-09-01"]
    assert result.context["source"] == "published_dataset"
    assert result.context["partitions"]["2026-08"]["version_id"] == ""


def test_indicators_report_published_input_version(tmp_path, monkeypatch):
    monkeypatch.delenv("MANAGEMENT_DB_PATH", raising=False)
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily()
    source = capture_frames(
        warehouse, dataset_name="stock_daily", source_name="tencent",
        frames=[pd.DataFrame({"date": pd.to_datetime(["2026-08-28"]), "code": ["sh600000"],
                              "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
                              "volume": [1.0], "amount": [0.1], "turn": [1.0]})],
        expected_symbols=1, success_symbols=1, universe_id="u", request_context={"fixture": True})
    build = DailyBuilder(warehouse).build_partition("2026-08", [("tencent", source["raw"]["path"])], include_current=False)
    state = PipelineState(warehouse.meta_db_path)
    version = state.create_version(build, source_batches=[source["batch_id"]])
    quality = check_stock_daily(build["path"], expected_symbols=1)
    state.quality(version, status=quality["status"], checks=quality["checks"], publish_allowed=True)
    Publisher(warehouse).publish(version)

    indicators = IndicatorsBuilder(warehouse, allow_legacy=False).build_all(max_symbols=1)
    assert indicators["input_versions"] == {"2026-08": version}
    assert indicators["input_fallback_used"] is False
    assert indicators["output_versions"]["2026-08"]
    indicator_result = load_dataset(warehouse, "indicators", "2026-08-28", "2026-08-28", required_quality="PASS")
    assert indicator_result.context["fallback_used"] is False
    assert indicator_result.context["partitions"]["2026-08"]["input_versions"]["stock_daily"]["2026-08"] == version
    with warehouse._conn() as conn:
        quality = conn.execute(
            "SELECT dataset_name, quality_status, input_versions FROM dataset_versions "
            "WHERE version_id = ?",
            (indicators["output_versions"]["2026-08"],),
        ).fetchall()
    assert {row[0]: row[1] for row in quality} == {"indicators": "PASS"}
    assert indicators["output_versions"]["2026-08"]
