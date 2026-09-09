"""Incremental coverage state for data-plane entities.

Raw files remain immutable evidence. This store is only the fast operational
index used to decide whether an entity needs another capture attempt.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class CoverageStore:
    """Persist latest coverage and date-level capture outcomes."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """CREATE TABLE IF NOT EXISTS dataset_entity_coverage (
                    dataset_name TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    last_success_date TEXT,
                    last_attempt_date TEXT,
                    last_status TEXT NOT NULL,
                    last_batch_id TEXT,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(dataset_name, source_name, entity_type, entity_id)
                );
                CREATE TABLE IF NOT EXISTS dataset_entity_date_status (
                    dataset_name TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    data_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    batch_id TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    error_code TEXT,
                    error_message TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(dataset_name, source_name, entity_type, entity_id, data_date)
                );
                CREATE INDEX IF NOT EXISTS idx_entity_coverage_lookup
                    ON dataset_entity_coverage(dataset_name, source_name, entity_type, entity_id);
                CREATE INDEX IF NOT EXISTS idx_entity_date_status_lookup
                    ON dataset_entity_date_status(dataset_name, source_name, entity_id, data_date);
                """
            )

    def latest_success_dates(self, dataset_name: str, source_name: str,
                             entity_ids: list[str], entity_type: str) -> dict[str, str]:
        if not entity_ids:
            return {}
        placeholders = ",".join("?" for _ in entity_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT entity_id,last_success_date FROM dataset_entity_coverage
                    WHERE dataset_name=? AND source_name=? AND entity_type=?
                      AND entity_id IN ({placeholders})
                      AND last_success_date IS NOT NULL""",
                (dataset_name, source_name, entity_type, *entity_ids),
            ).fetchall()
        return {str(row["entity_id"]): str(row["last_success_date"]) for row in rows}

    def record_attempt(self, *, dataset_name: str, source_name: str,
                       entity_type: str, entity_id: str, data_date: str,
                       status: str, batch_id: str | None = None,
                       error_code: str | None = None,
                       error_message: str | None = None) -> None:
        """Record one date outcome and update the fast latest-state index."""
        now = _now()
        with self._connect() as conn:
            previous = conn.execute(
                """SELECT attempt_count FROM dataset_entity_date_status
                   WHERE dataset_name=? AND source_name=? AND entity_type=?
                     AND entity_id=? AND data_date=?""",
                (dataset_name, source_name, entity_type, entity_id, data_date),
            ).fetchone()
            attempts = int(previous[0]) + 1 if previous else 1
            conn.execute(
                """INSERT INTO dataset_entity_date_status
                   (dataset_name,source_name,entity_type,entity_id,data_date,status,batch_id,
                    attempt_count,error_code,error_message,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(dataset_name,source_name,entity_type,entity_id,data_date)
                   DO UPDATE SET status=excluded.status,batch_id=excluded.batch_id,
                     attempt_count=excluded.attempt_count,error_code=excluded.error_code,
                     error_message=excluded.error_message,updated_at=excluded.updated_at""",
                (dataset_name, source_name, entity_type, entity_id, data_date, status,
                 batch_id, attempts, error_code, error_message, now),
            )
            current = conn.execute(
                """SELECT last_success_date,failure_count FROM dataset_entity_coverage
                   WHERE dataset_name=? AND source_name=? AND entity_type=? AND entity_id=?""",
                (dataset_name, source_name, entity_type, entity_id),
            ).fetchone()
            last_success = current[0] if current else None
            failures = int(current[1]) if current else 0
            if status == "success" and (not last_success or data_date > last_success):
                last_success = data_date
            if status in {"failed", "timeout", "empty", "manual_retry_required"}:
                failures += 1
            conn.execute(
                """INSERT INTO dataset_entity_coverage
                   (dataset_name,source_name,entity_type,entity_id,last_success_date,
                    last_attempt_date,last_status,last_batch_id,failure_count,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(dataset_name,source_name,entity_type,entity_id)
                   DO UPDATE SET last_success_date=excluded.last_success_date,
                     last_attempt_date=excluded.last_attempt_date,last_status=excluded.last_status,
                     last_batch_id=excluded.last_batch_id,failure_count=excluded.failure_count,
                     updated_at=excluded.updated_at""",
                (dataset_name, source_name, entity_type, entity_id, last_success,
                 data_date, status, batch_id, failures, now),
            )

    def record_success(self, *, dataset_name: str, source_name: str,
                       entity_type: str, entity_id: str, data_dates: list[str],
                       batch_id: str | None = None) -> None:
        for data_date in sorted(set(data_dates)):
            self.record_attempt(dataset_name=dataset_name, source_name=source_name,
                                entity_type=entity_type, entity_id=entity_id,
                                data_date=data_date, status="success", batch_id=batch_id)

    def record_failure(self, *, dataset_name: str, source_name: str,
                       entity_type: str, entity_id: str, data_date: str,
                       status: str = "failed", batch_id: str | None = None,
                       error_code: str | None = None,
                       error_message: str | None = None) -> None:
        self.record_attempt(dataset_name=dataset_name, source_name=source_name,
                            entity_type=entity_type, entity_id=entity_id,
                            data_date=data_date, status=status, batch_id=batch_id,
                            error_code=error_code, error_message=error_message)
