import sqlite3

from ops.data_center_service import DataCenterService
from ops.management_db import ManagementDB


def test_data_asset_detail_aggregates_dataset_consumers(tmp_path):
    path = tmp_path / "management.db"
    ManagementDB(path).seed_definitions()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO dataset_consumers VALUES(?,?,?,?,?,?,?,?,?,?)",
                     ("stock_daily", "market_scan", "business", None, "全市场筛选", "PASS", "published_only", "停止扫描", "now", "now"))
    asset = DataCenterService(path).asset("close")
    assert asset["input_datasets"]
    assert any(item["consumer"] == "market_scan" for item in asset["business_impact"])
