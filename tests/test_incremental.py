import pytest

from StockInvestmentTool.warehouse.incremental import affected_partitions, affected_window


def test_incremental_window_covers_lookback_and_changed_end():
    start, end = affected_window("2026-08-28", "2026-08-28", lookback_days=10)
    assert start == "2026-08-08"
    assert end == "2026-08-28"
    partitions = affected_partitions("2026-08-28", "2026-08-28")
    assert partitions[0] == "2025-04"
    assert partitions[-1] == "2026-08"


def test_incremental_window_rejects_reversed_range():
    with pytest.raises(ValueError):
        affected_window("2026-08-29", "2026-08-28")


def test_incremental_partitions_are_bounded_by_changed_range():
    partitions = affected_partitions("2026-08-28", "2026-09-02")
    assert partitions[-1] == "2026-09"
