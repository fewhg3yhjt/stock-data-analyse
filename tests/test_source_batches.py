import pandas as pd
import pytest

from StockInvestmentTool.warehouse.source_batches import SourceBatchStore
from StockInvestmentTool.warehouse.storage import Warehouse
from StockInvestmentTool.warehouse.collector import MarketCollector


def test_source_batch_has_unique_ids_and_traceable_stats(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    store = SourceBatchStore(warehouse.meta_db_path)
    first = store.start(run_date="2026-08-28", trade_date_start="2026-08-27",
                        trade_date_end="2026-08-28", expected_symbols=2,
                        universe_id="u1", request_context={"limit": 2})
    second = store.start(run_date="2026-08-28", trade_date_start="2026-08-27",
                         trade_date_end="2026-08-28", expected_symbols=2,
                         universe_id="u1", request_context={"limit": 2})
    assert first != second
    store.finish(first, success_symbols=2, failed_symbols=0, skipped_symbols=0,
                 row_count=2, raw_path="raw/a.parquet", checksum="abc", file_size=12,
                 status="success")
    row = store.get(first)
    assert row["status"] == "success"
    assert row["row_count"] == 2
    assert row["batch_id"] != row["raw_path"]


def test_source_batch_recovery_finishes_abandoned_running_row(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    store = SourceBatchStore(warehouse.meta_db_path)
    batch_id = store.start(run_date="2026-08-28", trade_date_start="2026-08-28",
                           trade_date_end="2026-08-28", expected_symbols=1,
                           universe_id="u1", request_context={})
    recovered = store.recover_running(before="2099-01-01T00:00:00")
    assert recovered == 1
    row = store.get(batch_id)
    assert row["status"] == "failed"
    assert row["finished_at"]


def test_raw_batch_is_atomic_and_immutable(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    frame = pd.DataFrame({"date": pd.to_datetime(["2026-08-28"]),
                          "code": ["sh600000"], "close": [10.0]})
    result = warehouse.raw.write_batch("tencent", "stock_daily", "2026-08-28", [frame])
    path = result["path"]
    assert path.exists()
    assert path.parts[-5:] == ("stock_daily", "2026", "08", "28", path.name)
    assert result["row_count"] == 1
    assert pd.read_parquet(path).iloc[0]["code"] == "sh600000"


def test_raw_batch_failure_leaves_no_final_file(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    writer = warehouse.raw.begin_batch("tencent", "stock_daily", "2026-08-28")
    writer.append(pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"]}))
    writer.abort()
    assert not list((warehouse.base_dir / "raw" / "tencent" / "stock_daily" / "2026" / "08" / "28").glob("batch_*.parquet"))


def test_raw_batch_empty_input_is_rejected(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    with pytest.raises(ValueError):
        warehouse.raw.write_batch("tencent", "stock_daily", "2026-08-28", [])


def test_daily_capture_rejects_legacy_daily_path(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    collector = MarketCollector(warehouse=warehouse, query_interval=0)
    with pytest.raises(ValueError, match="不再支持 target='daily'"):
        collector.sync_daily(start_date="2026-08-27", end_date="2026-08-28",
                              symbols=["sh600000"], source="tencent", target="daily")


def test_tencent_capture_preserves_source_values_in_raw(tmp_path, monkeypatch):
    warehouse = Warehouse(tmp_path / "warehouse")
    collector = MarketCollector(warehouse=warehouse, query_interval=0)

    def fake_fetch(code, start, end, request_timeout=None):
        return pd.DataFrame({
            "date": pd.to_datetime(["2026-08-28"]), "code": [code],
            "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
            "volume": [1000.0 if code == "sh600000" else 1000000.0],
            "amount": [102.0 if code == "sh600000" else 100000.0], "turn": [1.0],
        })

    monkeypatch.setattr(collector, "_fetch_symbol_tencent", fake_fetch)
    result = collector.sync_daily(
        start_date="2026-08-28", end_date="2026-08-28",
        symbols=["sh600000", "sh688007", "sh510300"], source="tencent",
        target="raw:tencent", capture_raw=True, flush_every=10,
    )
    raw = pd.read_parquet(result["raw_batch"]["path"])
    assert raw.set_index("code").loc["sh600000", "volume"] == 1000.0
    assert raw.set_index("code").loc["sh600000", "amount"] == 102.0
    assert raw.set_index("code").loc["sh688007", "volume"] == 1000000.0
    assert raw.set_index("code").loc["sh688007", "amount"] == 100000.0
    assert raw.set_index("code").loc["sh510300", "volume"] == 1000000.0
    assert raw.set_index("code").loc["sh510300", "amount"] == 100000.0
    assert "raw_volume_unit" not in raw.columns
    assert "raw_amount_unit" not in raw.columns


def test_stock_daily_capture_does_not_use_legacy_monthly_raw_writer(tmp_path, monkeypatch):
    warehouse = Warehouse(tmp_path / "warehouse")
    collector = MarketCollector(warehouse=warehouse, query_interval=0)

    def fail_monthly_writer(*args, **kwargs):
        raise AssertionError("stock_daily capture must use immutable Raw Batch writer")

    monkeypatch.setattr(warehouse.raw, "write", fail_monthly_writer)
    monkeypatch.setattr(collector, "_fetch_symbol_tencent", lambda code, start, end, request_timeout=None: pd.DataFrame({
        "date": [pd.Timestamp("2026-08-28")], "code": [code], "open": [10.0],
        "high": [10.5], "low": [9.8], "close": [10.2], "volume": [100.0],
        "amount": [102.0], "turn": [1.0],
    }))
    result = collector.sync_daily(
        start_date="2026-08-28", end_date="2026-08-28", symbols=["sh600000"],
        source="tencent", target="raw:tencent", capture_raw=True,
    )
    assert result["raw_batch"]["row_count"] == 1


def test_effective_raw_merges_by_key_and_new_data_wins(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    first = warehouse.raw.write_batch("tencent", "stock_daily", "2026-08-28", [pd.DataFrame({
        "date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
        "close": [10.0], "volume": [100.0], "amount": [102.0],
    })])
    second = warehouse.raw.write_batch("tencent", "stock_daily", "2026-08-29", [pd.DataFrame({
        "date": [pd.Timestamp("2026-08-28"), pd.Timestamp("2026-08-29")],
        "code": ["sh600000", "sh600000"], "close": [10.2, 10.3],
        "volume": [101.0, 102.0], "amount": [103.0, 104.0],
    })])
    warehouse.raw.merge_batch_to_effective("tencent", "stock_daily", first["path"])
    result = warehouse.raw.merge_batch_to_effective("tencent", "stock_daily", second["path"])
    effective = pd.read_parquet(result["2026-08"]["path"]).sort_values("date")
    assert len(effective) == 2
    assert effective.iloc[0]["close"] == 10.2
    assert effective.iloc[1]["close"] == 10.3


def test_invalid_source_format_is_manual_retry_and_not_written(tmp_path, monkeypatch):
    warehouse = Warehouse(tmp_path / "warehouse")
    collector = MarketCollector(warehouse=warehouse, query_interval=0)
    monkeypatch.setattr(collector, "_fetch_symbol_tencent", lambda code, start, end, request_timeout=None: pd.DataFrame({
        "date": [pd.Timestamp("2026-08-28")], "code": [code], "open": [10.0],
        "high": [10.5], "low": [9.8], "close": [10.2], "volume": [-1.0],
        "amount": [102.0], "turn": [1.0],
    }))
    result = collector.sync_daily(
        start_date="2026-08-28", end_date="2026-08-28", symbols=["sh600000"],
        source="tencent", target="raw:tencent", capture_raw=True,
    )
    assert result["failed"] == ["sh600000"]
    assert result["raw_capture_failed"] is True


def test_daily_capture_requires_explicit_date_range(tmp_path):
    collector = MarketCollector(warehouse=Warehouse(tmp_path / "warehouse"), query_interval=0)
    with pytest.raises(ValueError, match="必须显式传入"):
        collector.sync_daily(symbols=["sh600000"], source="tencent", target="raw:tencent")


def test_daily_capture_timeout_marks_unprocessed_symbols_failed(tmp_path, monkeypatch):
    warehouse = Warehouse(tmp_path / "warehouse")
    collector = MarketCollector(warehouse=warehouse, query_interval=0)
    calls = []

    def fake_fetch(code, start, end, request_timeout=None):
        calls.append(code)
        return pd.DataFrame({
            "date": pd.to_datetime(["2026-08-28"]), "code": [code],
            "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
            "volume": [100], "amount": [1000], "turn": [1.0],
        })

    monkeypatch.setattr(collector, "_fetch_symbol_tencent", fake_fetch)
    result = collector.sync_daily(
        start_date="2026-08-28", end_date="2026-08-28",
        symbols=["sh600000", "sh600001", "sh600002"], source="tencent",
        timeout=0,
    )

    assert result["timed_out"] is True
    assert result["status"] == "timeout"
    assert result["failed"] == ["sh600000", "sh600001", "sh600002"]
    assert calls == []


def test_daily_capture_resumes_from_partial_raw_batch(tmp_path, monkeypatch):
    warehouse = Warehouse(tmp_path / "warehouse")
    from StockInvestmentTool.warehouse.coverage import CoverageStore

    store = SourceBatchStore(warehouse.meta_db_path)
    partial = warehouse.raw.write_batch("tencent", "stock_daily", "2026-08-28", [
        pd.DataFrame({"date": pd.to_datetime(["2026-08-28"]), "code": ["sh600000"],
                      "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
                      "volume": [100], "amount": [1000], "turn": [1.0]})
    ])
    batch_id = store.start(run_date="2026-08-28", trade_date_start="2026-08-28",
                           trade_date_end="2026-08-28", expected_symbols=2,
                           universe_id="u1", request_context={}, source_name="tencent")
    store.finish(batch_id, success_symbols=1, failed_symbols=1, skipped_symbols=0,
                 row_count=1, raw_path=str(partial["path"]), checksum=partial["checksum"],
                 file_size=partial["file_size"], status="partial_success", failure_details=["sh600001"])
    CoverageStore(warehouse.meta_db_path).record_success(
        dataset_name="stock_daily", source_name="tencent", entity_type="stock",
        entity_id="sh600000", data_dates=["2026-08-28"], batch_id=batch_id,
    )
    collector = MarketCollector(warehouse=warehouse, query_interval=0)
    calls = []

    def fake_fetch(code, start, end, request_timeout=None):
        calls.append(code)
        return pd.DataFrame({"date": pd.to_datetime(["2026-08-28"]), "code": [code],
                             "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
                             "volume": [100], "amount": [1000], "turn": [1.0]})

    monkeypatch.setattr(collector, "_fetch_symbol_tencent", fake_fetch)
    result = collector.sync_daily(start_date="2026-08-28", end_date="2026-08-28",
                                  symbols=["sh600000", "sh600001"], source="tencent",
                                  target="raw:tencent", capture_raw=True)
    assert calls == ["sh600001"]
    assert result["skipped_symbols"] == 1


def test_daily_capture_uses_coverage_index_without_scanning_raw(tmp_path, monkeypatch):
    from StockInvestmentTool.warehouse.coverage import CoverageStore

    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.upsert_instruments([
        {"code": "sh600000", "type": "stock"},
        {"code": "sh510300", "type": "etf"},
    ])
    CoverageStore(warehouse.meta_db_path).record_success(
        dataset_name="stock_daily", source_name="tencent", entity_type="stock",
        entity_id="sh600000", data_dates=["2026-09-07"], batch_id="coverage-stock",
    )
    collector = MarketCollector(warehouse=warehouse, query_interval=0)
    calls = []

    def fake_fetch(code, start, end, request_timeout=None):
        calls.append((code, start, end))
        return pd.DataFrame({
            "date": pd.to_datetime(["2026-09-07"]), "code": [code],
            "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
            "volume": [100], "amount": [1000], "turn": [1.0],
        })

    monkeypatch.setattr(collector, "_fetch_symbol_tencent", fake_fetch)
    result = collector.sync_daily(
        start_date="2026-09-07", end_date="2026-09-07",
        symbols=["sh600000", "sh510300"], source="tencent", target="raw:tencent",
        capture_raw=True,
    )

    assert [item[0] for item in calls] == ["sh510300"]
    assert result["coverage_by_type"]["stock"]["skipped"] == 1
    assert result["coverage_by_type"]["etf"]["success"] == 1
