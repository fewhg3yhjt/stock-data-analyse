"""Shared capture helpers for non-stock_daily source datasets."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from StockInvestmentTool.warehouse.source_batches import SourceBatchStore


def capture_frames(warehouse, *, dataset_name: str, source_name: str,
                   frames: Iterable[pd.DataFrame], run_date: Optional[str] = None,
                   trade_date_start: Optional[str] = None, trade_date_end: Optional[str] = None,
                   expected_symbols: int = 0, success_symbols: int = 0,
                   failed_symbols: int = 0, skipped_symbols: int = 0,
                   universe_id: str = "", request_context: Optional[dict] = None,
                   job_run_id: Optional[int] = None, schema_version: str = "stock_daily.v1",
                   failure_details: Optional[list[str]] = None) -> dict:
    """Write one immutable raw batch and finish its SQLite ledger row."""
    run_date = run_date or datetime.now().strftime("%Y-%m-%d")
    batch_store = SourceBatchStore(warehouse.meta_db_path)
    batch_id = batch_store.start(
        dataset_name=dataset_name, source_name=source_name, run_date=run_date,
        trade_date_start=trade_date_start or run_date, trade_date_end=trade_date_end or run_date,
        expected_symbols=expected_symbols, universe_id=universe_id,
        request_context=request_context or {}, job_run_id=job_run_id, schema_version=schema_version,
    )
    writer = warehouse.raw.begin_batch(source_name, dataset_name, run_date)
    try:
        for frame in frames:
            writer.append(frame)
        raw = writer.finish()
        status = "partial_success" if failed_symbols else "success"
        batch_store.finish(batch_id, success_symbols=success_symbols, failed_symbols=failed_symbols,
                           skipped_symbols=skipped_symbols, row_count=raw["row_count"],
                           raw_path=str(raw["path"]), checksum=raw["checksum"],
                           file_size=raw["file_size"], status=status, failure_details=failure_details)
        return {"batch_id": batch_id, "raw": raw, "status": status}
    except Exception as exc:
        writer.abort()
        batch_store.finish(batch_id, success_symbols=success_symbols, failed_symbols=failed_symbols,
                           skipped_symbols=skipped_symbols, row_count=0, raw_path=None,
                           checksum=None, file_size=None, status="failed", error_summary=str(exc))
        raise
