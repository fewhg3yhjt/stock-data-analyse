import pandas as pd
from pathlib import Path

from StockInvestmentTool.warehouse.dataset_build import DatasetBuilder
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.storage import Warehouse


def test_auxiliary_dataset_builder_uses_yaml_schema_and_publishes_isolated_path(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_dataset("valuation_daily")
    raw = warehouse.base_dir / "raw.parquet"
    pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
                  "peTTM": [6.2], "pbMRQ": [0.82]}).to_parquet(raw, index=False)
    build = DatasetBuilder(warehouse, "valuation_daily").build("2026-08", [("eastmoney", raw)])
    state = PipelineState(warehouse.meta_db_path)
    version = state.create_version(build, dataset_name="valuation_daily", schema_version="valuation_daily.v1", source_batches=[])
    with warehouse._conn() as conn:
        conn.execute("UPDATE dataset_versions SET quality_status='PASS' WHERE version_id=?", (version,))
    result = Publisher(warehouse).publish(version)
    assert result["path"].endswith("raw/valuation/2026-08.parquet")
    assert Path(result["path"]).exists()
