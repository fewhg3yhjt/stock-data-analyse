import pandas as pd
from pathlib import Path

from scripts.build_migration_candidates import build_candidates


def test_build_candidates_writes_only_to_output(tmp_path):
    warehouse = tmp_path / "warehouse"
    source_dir = warehouse / "daily"
    source_dir.mkdir(parents=True)
    source = source_dir / "2026-01.parquet"
    pd.DataFrame({"date": pd.to_datetime(["2026-01-02"]), "code": ["sh600000"],
                  "open": [1], "high": [2], "low": [1], "close": [1.5],
                  "volume": [1], "amount": [2]}).to_parquet(source, index=False)
    report = build_candidates(warehouse, tmp_path / "candidates", ["stock_daily"])
    assert report["records"][0]["status"] == "candidate"
    assert Path(report["records"][0]["candidate_path"]).exists()


def test_build_candidates_excludes_legacy_fundamental_outputs(tmp_path):
    warehouse = tmp_path / "warehouse" / "fundamentals"
    warehouse.mkdir(parents=True)
    pd.DataFrame({"stat_date": ["2026-03-31"], "roe": [8.0]}).to_parquet(
        warehouse / "sh600000.parquet", index=False)
    pd.DataFrame({"stat_date": ["2026-03-31"], "roe": [8.0]}).to_parquet(
        warehouse / "legacy_sh600000_old.parquet", index=False)
    report = build_candidates(tmp_path / "warehouse", tmp_path / "candidates", ["fundamentals"])
    assert [Path(item["source_path"]).name for item in report["records"]] == ["sh600000.parquet"]
