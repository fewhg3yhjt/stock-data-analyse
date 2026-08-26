"""Durable scheduler job execution ledger."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional


class JobRunStore:
    def __init__(self, db_path: Optional[Path | str] = None):
        if db_path is None:
            from StockInvestmentTool.config import Config
            db_path = Config.DATA_DIR / "job_runs.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS job_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                result TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT ''
            )""")

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def start(self, job_name: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO job_runs(job_name,started_at) VALUES(?,?)",
                (job_name, datetime.now().isoformat(timespec="seconds")),
            )
            return int(cur.lastrowid)

    def finish(self, run_id: int, status: str = "success", result: Optional[dict] = None,
               error: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE job_runs SET finished_at=?,status=?,result=?,error=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), status,
                 json.dumps(result or {}, ensure_ascii=False, default=str), str(error)[:2000], run_id),
            )

    def recent(self, limit: int = 30) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["result"] = json.loads(item["result"])
            except Exception:
                item["result"] = {}
            out.append(item)
        return out
