"""Source Batch ledger for traceable raw captures."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SourceBatchStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS source_batches (
                batch_id TEXT PRIMARY KEY, dataset_name TEXT NOT NULL, source_name TEXT NOT NULL,
                job_run_id INTEGER, run_date TEXT NOT NULL, trade_date_start TEXT,
                trade_date_end TEXT, universe_id TEXT, expected_symbols INTEGER NOT NULL,
                success_symbols INTEGER NOT NULL DEFAULT 0, failed_symbols INTEGER NOT NULL DEFAULT 0,
                skipped_symbols INTEGER NOT NULL DEFAULT 0, row_count INTEGER NOT NULL DEFAULT 0,
                raw_path TEXT, schema_version TEXT NOT NULL, request_context TEXT,
                checksum TEXT, file_size INTEGER, status TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, error_summary TEXT, failure_details TEXT
            )""")

    def start(self, *, dataset_name: str = "stock_daily", run_date: str, trade_date_start: str, trade_date_end: str,
              expected_symbols: int, universe_id: str, request_context: dict,
               job_run_id: Optional[int] = None, source_name: str = "tencent",
               schema_version: str = "stock_daily.v1") -> str:
        batch_id = f"{source_name}_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:10]}"
        with self._connect() as conn:
            conn.execute("""INSERT INTO source_batches
                (batch_id,dataset_name,source_name,job_run_id,run_date,trade_date_start,trade_date_end,
                 universe_id,expected_symbols,request_context,schema_version,status,started_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (batch_id, dataset_name, source_name, job_run_id, run_date,
                 trade_date_start, trade_date_end, universe_id, expected_symbols,
                 json.dumps(request_context, ensure_ascii=False, default=str),
                  schema_version, "running", _now()))
        return batch_id

    def finish(self, batch_id: str, *, success_symbols: int, failed_symbols: int,
               skipped_symbols: int, row_count: int, raw_path: Optional[str],
               checksum: Optional[str], file_size: Optional[int], status: str,
               error_summary: str = "", failure_details: Optional[list[str]] = None) -> None:
        with self._connect() as conn:
            conn.execute("""UPDATE source_batches SET success_symbols=?,failed_symbols=?,
                skipped_symbols=?,row_count=?,raw_path=?,checksum=?,file_size=?,status=?,
                finished_at=?,error_summary=?,failure_details=? WHERE batch_id=?""",
                (success_symbols, failed_symbols, skipped_symbols, row_count, raw_path,
                 checksum, file_size, status, _now(), error_summary[:2000],
                 json.dumps(failure_details or [], ensure_ascii=False), batch_id))

    def get(self, batch_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM source_batches WHERE batch_id=?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def recover_running(self, *, before: datetime | str) -> int:
        """Finish batches abandoned by a terminated worker process."""
        boundary = before.isoformat(timespec="seconds") if isinstance(before, datetime) else str(before)
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE source_batches SET status='failed', finished_at=?,
                   error_summary='采集进程已结束，SourceBatch 自动回收'
                   WHERE status='running' AND started_at<?""",
                (now, boundary),
            )
        return cur.rowcount
