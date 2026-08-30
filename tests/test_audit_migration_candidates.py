import pandas as pd

from scripts.audit_migration_candidates import audit


def test_candidate_audit_checks_duplicate_keys(tmp_path):
    root = tmp_path / "candidates" / "stock_daily"
    root.mkdir(parents=True)
    pd.DataFrame({"date": pd.to_datetime(["2026-01-02", "2026-01-02"]),
                  "code": ["sh600000", "sh600000"], "close": [1, 1]}).to_parquet(root / "x.parquet")
    result = audit(tmp_path / "candidates")
    assert result["datasets"]["stock_daily"]["fail_count"] == 1
