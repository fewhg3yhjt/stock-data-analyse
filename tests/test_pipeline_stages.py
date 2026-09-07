import json
import hashlib
import sqlite3

import pandas as pd
import pytest

from StockInvestmentTool.warehouse.daily_build import DailyBuilder
from StockInvestmentTool.warehouse.datasets import DatasetAccessError
from StockInvestmentTool.warehouse.indicators_build import IndicatorsBuilder
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.quality import check_stock_daily, check_derived_output
from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.storage import Warehouse
from scripts.convert_legacy_daily import convert


def _publish_stock_daily(warehouse: Warehouse) -> str:
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
    Publisher(warehouse).publish(version)
    return version


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


def test_builder_respects_batch_share_yuan_units_without_second_conversion(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily()
    source = capture_frames(
        warehouse, dataset_name="stock_daily", source_name="tencent",
        frames=[pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
                              "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
                              "volume": [100.0], "amount": [1020.0], "turn": [1.0]})],
        expected_symbols=1, success_symbols=1, universe_id="u",
        request_context={"units": {"volume": "share", "amount": "yuan", "resolution": "fixture"}},
    )
    build = DailyBuilder(warehouse).build_partition(
        "2026-08", [("tencent", source["raw"]["path"], source["batch_id"])], include_current=False,
    )
    result = pd.read_parquet(build["path"])
    assert result.iloc[0]["volume"] == 100.0
    assert result.iloc[0]["amount"] == 1020.0


def test_builder_rejects_unknown_raw_units(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily()
    source = capture_frames(
        warehouse, dataset_name="stock_daily", source_name="tencent",
        frames=[pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
                              "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
                              "volume": [1.0], "amount": [0.1]})],
        expected_symbols=1, success_symbols=1, universe_id="u",
        request_context={"units": {"volume": "unknown", "amount": "yuan"}},
    )
    with pytest.raises(ValueError, match="单位未识别"):
        DailyBuilder(warehouse).build_partition(
            "2026-08", [("tencent", source["raw"]["path"], source["batch_id"])], include_current=False,
        )


def test_builder_rejects_historical_tencent_batch_without_units(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily()
    source = capture_frames(
        warehouse, dataset_name="stock_daily", source_name="tencent",
        frames=[pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
                              "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
                              "volume": [1.0], "amount": [0.1]})],
        expected_symbols=1, success_symbols=1, universe_id="u", request_context={"fixture": True},
    )
    with warehouse._conn() as conn:
        conn.execute("UPDATE source_batches SET request_context='{}' WHERE batch_id=?", (source["batch_id"],))
    with pytest.raises(ValueError, match="缺少单位元数据"):
        DailyBuilder(warehouse).build_partition(
            "2026-08", [("tencent", source["raw"]["path"], source["batch_id"])], include_current=False,
        )


def test_builder_enforces_tencent_68_market_share_contract(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_stock_daily()
    source = capture_frames(
        warehouse, dataset_name="stock_daily", source_name="tencent",
        frames=[pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh689009"],
                              "open": [40.0], "high": [41.0], "low": [39.0], "close": [40.0],
                              "volume": [1000000.0], "amount": [40000.0],
                              "raw_volume_unit": ["hand"], "raw_amount_unit": ["wan_yuan"]})],
        expected_symbols=1, success_symbols=1, universe_id="u",
        request_context={"units": {"volume": "share", "amount": "wan_yuan"}},
    )
    build = DailyBuilder(warehouse).build_partition(
        "2026-08", [("tencent", source["raw"]["path"], source["batch_id"])], include_current=False,
    )
    result = pd.read_parquet(build["path"])
    assert result.iloc[0]["volume"] == 1000000.0


def test_quality_fails_amount_volume_unit_mismatch(tmp_path):
    path = tmp_path / "unit_bad.parquet"
    pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
                  "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
                  "volume": [100.0], "amount": [102000.0]}).to_parquet(path, index=False)
    result = check_stock_daily(path, expected_symbols=1)
    assert result["status"] == "FAIL"
    assert result["checks"]["unit_consistency"]["abnormal_count"] == 1
    assert result["checks"]["unit_consistency"]["max"] == 5.0


# ── 阶段三：真实覆盖率与派生数据质量 ─────────────────────────

def test_derived_empty_partition_is_failed(tmp_path):
    path = tmp_path / "empty.parquet"
    pd.DataFrame({"date": [], "code": []}).to_parquet(path, index=False)
    result = check_derived_output(path, "indicators")
    assert result["status"] == "FAIL"
    assert result["publish_allowed"] is False


def test_derived_missing_required_columns_is_failed(tmp_path):
    path = tmp_path / "nocol.parquet"
    pd.DataFrame({"date": [pd.Timestamp("2026-08-28")]}).to_parquet(path, index=False)
    result = check_derived_output(path, "indicators")
    assert result["status"] == "FAIL"
    assert "code" in result["checks"]["missing_columns"]


def test_derived_low_coverage_is_failed(tmp_path):
    path = tmp_path / "low.parquet"
    rows = [{"date": pd.Timestamp("2026-08-28"), "code": f"sh{i:06d}",
             "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
             "volume": 100.0, "amount": 1000.0} for i in range(100)]
    pd.DataFrame(rows).to_parquet(path, index=False)
    result = check_derived_output(path, "indicators", expected_symbols=1000)
    assert result["status"] == "FAIL"
    assert result["checks"]["coverage"] == 0.1


def test_derived_core_columns_all_null_flagged_on_long_history(tmp_path):
    path = tmp_path / "nann.parquet"
    import numpy as np
    rows = [{"date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
             "code": "sh600000", "ma5": np.nan} for i in range(60)]
    pd.DataFrame(rows).to_parquet(path, index=False)
    result = check_derived_output(path, "indicators", core_non_null_columns=["ma5"])
    assert result["status"] == "WARNING"


def test_indicators_builder_blocks_on_missing_published_input(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    with pytest.raises(DatasetAccessError, match="没有 Published Dataset"):
        IndicatorsBuilder(warehouse, allow_legacy=False).build_all(months=["2026-08"])


def test_record_output_versions_leaves_candidate_not_published(tmp_path):
    from StockInvestmentTool.warehouse.indicators_build import IndicatorsBuilder
    warehouse = Warehouse(tmp_path / "warehouse")
    state = PipelineState(warehouse.meta_db_path)
    path = tmp_path / "ind.parquet"
    pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
                  "ma5": [10.2]}).to_parquet(path, index=False)
    versions = state.record_output_versions(
        dataset_name="indicators", paths={"2026-08": path},
        input_dataset="stock_daily", input_versions={},
        builder_version="test", schema_version="indicators.v1",
    )
    with sqlite3.connect(warehouse.meta_db_path) as conn:
        row = conn.execute("SELECT quality_status,publish_status,published_path FROM dataset_versions WHERE version_id=?",
                           (versions["2026-08"],)).fetchone()
    assert row[0] is None          # 质量未经检查
    assert row[1] == "candidate"   # 不自动发布
    assert row[2] is None          # 无正式路径
    assert state.current("indicators", "2026-08") is None


def test_extend_load_months_expands_narrow_window():
    from StockInvestmentTool.warehouse.indicators_build import _extend_load_months
    extended = _extend_load_months(["2026-09"])
    assert "2026-09" in extended
    # 至少向前扩展覆盖 ma240 需要的 ~12 个月历史。
    assert len(extended) >= 13
    assert extended[0] == "2025-07"


def test_indicators_build_narrow_window_keeps_ma_columns(tmp_path):
    """Incremental (single-month) build must still produce real MA history.

    Loads stock_daily from a multi-month history so rolling indicators are
    computable, and keeps the standard columns even if some rows are NaN.
    """
    from StockInvestmentTool.warehouse.indicators_build import IndicatorsBuilder
    warehouse = Warehouse(tmp_path / "warehouse", meta_db_path=tmp_path / "management.db")
    dates = pd.bdate_range("2026-05-01", periods=120)
    frame = pd.DataFrame({
        "date": dates, "code": ["sh600000"] * len(dates),
        "open": [10.0] * len(dates), "high": [10.5] * len(dates),
        "low": [9.8] * len(dates), "close": [10.0] * len(dates),
        "volume": [1.0] * len(dates), "amount": [0.1] * len(dates),
        "turn": [1.0] * len(dates),
    })
    # 按月份写分区
    for month, grp in frame.groupby(frame["date"].dt.strftime("%Y-%m")):
        warehouse.write_daily_partition(month, grp)
    warehouse.metadata.register_dataset("stock_daily")
    from StockInvestmentTool.warehouse.pipeline_state import PipelineState
    from StockInvestmentTool.warehouse.publish import Publisher
    from StockInvestmentTool.warehouse.quality import check_stock_daily
    from StockInvestmentTool.warehouse.daily_build import DailyBuilder
    state = PipelineState(warehouse.meta_db_path)
    # 逐月发布
    for month in sorted(frame["date"].dt.strftime("%Y-%m").unique()):
        path = warehouse.daily_partition(month)
        build = DailyBuilder(warehouse).build_partition(month, [("tencent", path)], include_current=False)
        version = state.create_version(build, source_batches=[], dataset_name="stock_daily", schema_version="stock_daily.v1")
        report = check_stock_daily(build["path"], expected_symbols=1)
        state.quality(version, status=report["status"], checks=report["checks"], publish_allowed=True)
        if report["publish_allowed"]:
            Publisher(warehouse).publish(version)
    result = IndicatorsBuilder(warehouse, allow_legacy=False).build_all(
        symbols=["sh600000"], months=["2026-09"], asset_types=["stock"])
    # 输出只写 2026-09
    assert "2026-09" in result.get("output_versions", {})
    ind = warehouse.read_indicator("2026-09")
    assert ind is not None and not ind.empty
    # ma5 列必须保留，且存在非 NaN 的真实值（历史来自扩展载入窗口）
    assert "ma5" in ind.columns
    assert ind["ma5"].notna().any()
