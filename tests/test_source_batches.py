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


def test_small_batch_tencent_capture_keeps_legacy_daily_path(tmp_path, monkeypatch):
    warehouse = Warehouse(tmp_path / "warehouse")
    collector = MarketCollector(warehouse=warehouse, query_interval=0)

    def fake_fetch(code, start, end):
        if code == "sz000001":
            raise ConnectionError("simulated timeout")
        return pd.DataFrame({
            "date": pd.to_datetime(["2026-08-27", "2026-08-28"]),
            "code": [code, code], "open": [10.0, 10.2], "high": [10.5, 10.6],
            "low": [9.8, 10.0], "close": [10.2, 10.4], "volume": [100, 120],
            "amount": [1000, 1200], "turn": [1.0, 1.2], "peTTM": [None, None],
            "pbMRQ": [None, None], "tradestatus": [None, None],
        })

    monkeypatch.setattr(collector, "_fetch_symbol_tencent", fake_fetch)
    result = collector.sync_daily(
        start_date="2026-08-27", end_date="2026-08-28",
        symbols=["sh600000", "sz000001", "sh600001"], source="tencent",
        target="daily", flush_every=1, job_run_id=42,
    )

    assert result["raw_capture_failed"] is False
    assert result["source_batch_id"]
    assert result["failed"] == ["sz000001"]
    assert len(warehouse.read_daily("2026-08")) == 4
    batch = SourceBatchStore(warehouse.meta_db_path).get(result["source_batch_id"])
    assert batch["status"] == "partial_success"
    assert batch["job_run_id"] == 42
    assert batch["expected_symbols"] == 3
    assert batch["success_symbols"] == 2
    assert batch["failed_symbols"] == 1
    assert batch["skipped_symbols"] == 0
    assert batch["row_count"] == 4
    assert pd.read_parquet(batch["raw_path"]).shape[0] == 4


def test_daily_capture_requires_explicit_date_range(tmp_path):
    collector = MarketCollector(warehouse=Warehouse(tmp_path / "warehouse"), query_interval=0)
    with pytest.raises(ValueError, match="必须显式传入"):
        collector.sync_daily(symbols=["sh600000"], source="tencent", target="raw:tencent")
