"""Coverage grouping uses existing asset type and board dimensions."""

from __future__ import annotations

import json
import sqlite3

from StockInvestmentTool.warehouse.source_batches import SourceBatchStore


def test_source_batch_context_can_store_classified_coverage(tmp_path):
    db = tmp_path / "meta.db"
    store = SourceBatchStore(db)
    batch = store.start(
        run_date="2026-09-07", trade_date_start="2026-09-07", trade_date_end="2026-09-07",
        expected_symbols=3, universe_id="test", request_context={"asset_types": ["stock", "etf"]},
    )
    store.update_request_context(batch, {
        "coverage_by_type": {"stock": {"expected": 2, "success": 1, "failed": 1, "skipped": 0},
                              "etf": {"expected": 1, "success": 1, "failed": 0, "skipped": 0}},
        "coverage_by_board": {"main_sh": {"expected": 2, "success": 1, "failed": 1, "skipped": 0}},
    })
    with sqlite3.connect(db) as conn:
        context = json.loads(conn.execute("SELECT request_context FROM source_batches WHERE batch_id=?", (batch,)).fetchone()[0])
    assert context["coverage_by_type"]["stock"]["failed"] == 1
    assert context["coverage_by_type"]["etf"]["success"] == 1
