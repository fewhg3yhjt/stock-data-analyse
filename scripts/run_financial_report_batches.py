"""Advance the explicit financial-report checkpoint in bounded serial batches."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from StockInvestmentTool.config import Config
from StockInvestmentTool.ops.task_execution import execute_task
from StockInvestmentTool.warehouse.storage import Warehouse


def main() -> int:
    db_path = Config.DATA_DIR / "management.db"
    warehouse = Warehouse(meta_db_path=db_path)
    symbols = [item["code"] for item in warehouse.list_instruments(asset_types=["stock"])]
    if not symbols:
        raise RuntimeError("没有可执行的股票范围")
    start_date = "2025-01-01"
    end_date = "2026-09-04"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT batch_id FROM source_batches WHERE dataset_name='financial_reports' "
            "AND trade_date_start=? AND trade_date_end=? ORDER BY started_at DESC LIMIT 1",
            (start_date, end_date),
        ).fetchone()
    checkpoint = row[0] if row else None

    while True:
        payload = {
            "trigger_type": "manual",
            "requested_by": "production_financial_report_batches",
            "period_start": start_date,
            "period_end": end_date,
            "request_timeout": 15,
            "task_timeout": 1500,
            "query_interval": 1.0,
            "failure_threshold": 20,
            "financial_batch_size": 100,
            "checkpoint_batch_id": checkpoint,
            "symbols": symbols,
        }
        result = execute_task(db_path, "financial_reports_capture", payload)
        print(result, flush=True)
        if result.get("status") not in {"success", "partial_success"}:
            return 1
        detail = result.get("result") or {}
        checkpoint = detail.get("source_batch_id") or checkpoint
        if detail.get("batch_complete"):
            return 0
        time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
