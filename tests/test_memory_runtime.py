"""Runtime memory diagnostics tests."""

from __future__ import annotations

from StockInvestmentTool.runtime.memory import memory_snapshot, monitor_memory


def test_memory_snapshot_has_process_identity_fields():
    snapshot = memory_snapshot()

    assert snapshot["rss_bytes"] is not None
    assert snapshot["rss_mb"] >= 0
    assert "container_mb" in snapshot


def test_monitor_memory_records_start_peak_and_end():
    with monitor_memory("test", interval=0.01) as state:
        pass

    result = state["state"]["result"]
    assert result["start"]["rss_bytes"] is not None
    assert result["peak"]["rss_bytes"] >= result["start"]["rss_bytes"]
    assert result["end"]["rss_bytes"] is not None
