import json

from scripts.reconcile_migration_candidates import reconcile


def test_metadata_reconciliation_detects_row_delta(tmp_path):
    source = {"datasets": {"daily": {"records": [{"path": "/src/a.parquet", "row_count": 2, "symbol_count": 1}]}}}
    candidates = {"records": [{"dataset": "stock_daily", "source_path": "/src/a.parquet", "row_count": 2, "symbol_count": 1},
                               {"dataset": "fundamentals", "source_path": "/src/a.parquet", "row_count": 1, "symbol_count": 1},
                               {"dataset": "valuation_daily", "source_path": "/src/a.parquet", "row_count": 2, "symbol_count": 1}]}
    source_path = tmp_path / "source.json"; source_path.write_text(json.dumps(source))
    candidate_path = tmp_path / "candidate.json"; candidate_path.write_text(json.dumps(candidates))
    report = reconcile(source_path, candidate_path)
    assert report["datasets"]["stock_daily"]["status"] == "PASS"
    assert report["datasets"]["fundamentals"]["status"] == "WARNING"
