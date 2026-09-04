"""Static public export is derived exclusively from Published stock_daily."""

import json

import pandas as pd

from StockInvestmentTool.warehouse.daily_build import DailyBuilder
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.quality import check_stock_daily
from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.storage import Warehouse


def test_export_public_daily_writes_atomic_static_json_from_published_data(tmp_path, monkeypatch):
    monkeypatch.delenv("MANAGEMENT_DB_PATH", raising=False)
    warehouse = Warehouse(tmp_path / "data" / "warehouse")
    warehouse.metadata.register_stock_daily()
    warehouse.upsert_instruments([{"code": "sz000400", "name": "许继电气", "type": "stock"}])
    frame = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-01", "2026-09-02"]), "code": ["sz000400", "sz000400"],
        "open": [21.0, 21.1], "high": [21.5, 21.6], "low": [20.8, 20.9], "close": [21.3, 21.4],
        "volume": [100.0, 200.0], "amount": [2100.0, 4300.0], "turn": [1.1, 1.2],
    })
    source = capture_frames(warehouse, dataset_name="stock_daily", source_name="tencent", frames=[frame],
                            expected_symbols=1, success_symbols=1, universe_id="fixture", request_context={})
    build = DailyBuilder(warehouse).build_partition("2026-09", [("tencent", source["raw"]["path"])], include_current=False)
    version = PipelineState(warehouse.meta_db_path).create_version(build, source_batches=[source["batch_id"]])
    quality = check_stock_daily(build["path"], expected_symbols=1)
    PipelineState(warehouse.meta_db_path).quality(version, status=quality["status"], checks=quality["checks"], publish_allowed=True)
    published = Publisher(warehouse).publish(version)

    path = tmp_path / "public-data" / "000400.json"
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert published["public_exports"]["exports"][0]["code"] == "000400"
    assert payload["name"] == "许继电气"
    assert payload["count"] == 2
    assert payload["data"][0]["turnover_rate"] == 1.1
    assert payload["meta"]["source"] == "published_dataset"
