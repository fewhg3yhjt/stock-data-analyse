from __future__ import annotations

import pandas as pd

from StockInvestmentTool.market_discovery.service import discover_stocks, stock_series
from StockInvestmentTool.market_discovery.storage import DiscoveryRunStore
from StockInvestmentTool.warehouse.storage import Warehouse


def _daily_frame():
    dates = pd.date_range("2026-01-01", periods=90, freq="B")
    rows = []
    for code, closes, volumes in (
        ("sh600900", list(range(10, 100)), list(range(1000, 910, -1)),),
        ("sz000001", list(range(100, 10, -1)), list(range(1000, 1090)),),
    ):
        for day, close, volume in zip(dates, closes, volumes):
            rows.append({"date": day, "code": code, "open": close,
                         "high": close + 1, "low": close - 1,
                         "close": close, "volume": volume, "amount": close * volume})
    return pd.DataFrame(rows)


def test_discover_stocks_returns_explainable_volume_signal(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    frame = _daily_frame()
    warehouse.write_daily_partition("2026-04", frame)
    result = discover_stocks({"lookback_days": 3, "min_up_days": 2,
                              "signal": "price_up_volume_down"},
                              warehouse=warehouse, top_n=10, allow_legacy=True)
    assert result["count"] == 1
    assert result["total_count"] == 1
    assert result["items"][0]["code"] == "sh600900"
    assert "上涨缩量" in result["items"][0]["signal_tags"]
    assert result["items"][0]["name"] == "sh600900"


def test_empty_down_days_does_not_filter_valid_up_days(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.write_daily_partition("2026-04", _daily_frame())
    result = discover_stocks({"lookback_days": 3, "min_up_days": 2},
                             warehouse=warehouse, top_n=10, allow_legacy=True)
    assert result["total_count"] == 1
    assert result["items"][0]["code"] == "sh600900"


def test_stock_series_uses_local_daily_data(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.write_daily_partition("2026-04", _daily_frame())
    result = stock_series("sh600900", warehouse=warehouse, days=20, allow_legacy=True)
    assert len(result["dates"]) == 20
    assert result["close"][-1] == 99
    assert result["open"][-1] == 99


def test_discovery_run_store_records_conditions(tmp_path):
    store = DiscoveryRunStore(tmp_path / "runs.db")
    run_id = store.save(as_of="2026-05-06", conditions={"signal": "volume_spike"}, result_count=2)
    assert store.recent(1)[0]["id"] == run_id
    assert store.recent(1)[0]["conditions"]["signal"] == "volume_spike"


def test_discover_stocks_uses_published_industry_membership(tmp_path, monkeypatch):
    warehouse = Warehouse(tmp_path / "warehouse")
    frame = _daily_frame()
    warehouse.write_daily_partition("2026-04", frame)
    warehouse.metadata.register_dataset("industry_membership")
    membership = pd.DataFrame({
        "snapshot_date": ["2026-04-30", "2026-04-30"],
        "code": ["sh600900", "sz000001"],
        "industry_code": ["E47", "E47"],
        "industry_name": ["房屋建筑业", "房屋建筑业"],
        "raw_industry": ["E47房屋建筑业", "E47房屋建筑业"],
        "industry_classification": ["csrc", "csrc"],
        "source_update_date": [None, None], "source": ["test", "test"],
        "captured_at": ["2026-04-30T00:00:00"] * 2,
    })
    raw = tmp_path / "membership.parquet"
    membership.to_parquet(raw, index=False)
    from StockInvestmentTool.warehouse.pipeline_state import PipelineState
    from StockInvestmentTool.warehouse.quality import check_industry_membership
    from StockInvestmentTool.warehouse.source_capture import capture_frames
    from StockInvestmentTool.warehouse.publish import Publisher
    captured = capture_frames(warehouse, dataset_name="industry_membership", source_name="test", frames=[membership], run_date="2026-04-30", expected_symbols=2, success_symbols=2, universe_id="test", request_context={})
    from StockInvestmentTool.warehouse.industry import build_industry_candidate
    build = build_industry_candidate(warehouse, "industry_membership", "2026-04-30", captured["raw"]["path"])
    state = PipelineState(warehouse.meta_db_path)
    version = state.create_version(build, source_batches=[captured["batch_id"]], dataset_name="industry_membership", schema_version="industry_membership.v1")
    quality = check_industry_membership(build["path"], expected_symbols=2)
    state.quality(version, status=quality["status"], checks=quality["checks"], publish_allowed=True)
    Publisher(warehouse).publish(version)
    result = discover_stocks({"industry": "E47房屋建筑业", "lookback_days": 3, "min_history": 20}, warehouse=warehouse, top_n=10, allow_legacy=True)
    assert result["total_count"] == 2


def test_discover_stocks_displays_published_industry_membership(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.write_daily_partition("2026-04", _daily_frame())
    warehouse.metadata.register_dataset("industry_membership")
    membership = pd.DataFrame({
        "snapshot_date": ["2026-04-30"], "code": ["sh600900"],
        "industry_code": ["D44"], "industry_name": ["电力、热力生产和供应业"],
        "raw_industry": ["D44电力、热力生产和供应业"],
        "industry_classification": ["csrc"], "source_update_date": [None],
        "source": ["test"], "captured_at": ["2026-04-30T00:00:00"],
    })
    from StockInvestmentTool.warehouse.source_capture import capture_frames
    from StockInvestmentTool.warehouse.industry import build_industry_candidate
    from StockInvestmentTool.warehouse.pipeline_state import PipelineState
    from StockInvestmentTool.warehouse.quality import check_industry_membership
    from StockInvestmentTool.warehouse.publish import Publisher
    captured = capture_frames(warehouse, dataset_name="industry_membership", source_name="test", frames=[membership], run_date="2026-04-30", expected_symbols=1, success_symbols=1, universe_id="test", request_context={})
    build = build_industry_candidate(warehouse, "industry_membership", "2026-04-30", captured["raw"]["path"])
    state = PipelineState(warehouse.meta_db_path)
    version = state.create_version(build, source_batches=[captured["batch_id"]], dataset_name="industry_membership", schema_version="industry_membership.v1")
    quality = check_industry_membership(build["path"], expected_symbols=1)
    state.quality(version, status=quality["status"], checks=quality["checks"], publish_allowed=True)
    Publisher(warehouse).publish(version)
    result = discover_stocks({"keyword": "sh600900", "lookback_days": 3, "min_history": 20}, warehouse=warehouse, top_n=10, allow_legacy=True)
    assert result["items"][0]["industry"] == "D44电力、热力生产和供应业"
