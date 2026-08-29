from datetime import datetime

import pytest

from scripts.run_shadow_pipeline import _business_end, run_shadow


def test_shadow_run_rejects_formal_warehouse_and_large_scope(tmp_path):
    with pytest.raises(ValueError, match="最多允许 100"):
        run_shadow(tmp_path / "shadow", [f"sh{i:06d}" for i in range(101)], "2026-08-24", "2026-08-28", ["stock"])


def test_shadow_business_end_skips_weekend():
    assert _business_end(datetime(2026, 8, 31)).strftime("%Y-%m-%d") == "2026-08-28"
