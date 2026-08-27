"""Durable scheduler job execution ledger."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, date
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
            self._ensure_columns(conn)
            conn.execute("""CREATE TABLE IF NOT EXISTS job_plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT NOT NULL,
                task_key TEXT NOT NULL,
                display_name TEXT NOT NULL,
                scheduled_at TEXT,
                status TEXT NOT NULL DEFAULT 'scheduled',
                phase TEXT NOT NULL DEFAULT '',
                progress INTEGER NOT NULL DEFAULT 0,
                processed INTEGER,
                total INTEGER,
                current_item TEXT NOT NULL DEFAULT '',
                input_dataset TEXT NOT NULL DEFAULT '',
                output_dataset TEXT NOT NULL DEFAULT '',
                blocked_by TEXT NOT NULL DEFAULT '',
                run_id INTEGER,
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                UNIQUE(run_date, task_key)
            )""")

    @staticmethod
    def _ensure_columns(conn):
        columns = {row[1] for row in conn.execute("PRAGMA table_info(job_runs)").fetchall()}
        additions = {
            "run_date": "TEXT", "display_name": "TEXT NOT NULL DEFAULT ''",
            "scheduled_at": "TEXT", "phase": "TEXT NOT NULL DEFAULT ''",
            "progress": "INTEGER NOT NULL DEFAULT 0", "processed": "INTEGER",
            "total": "INTEGER", "current_item": "TEXT NOT NULL DEFAULT ''",
            "input_dataset": "TEXT NOT NULL DEFAULT ''", "output_dataset": "TEXT NOT NULL DEFAULT ''",
            "parent_run_id": "INTEGER", "updated_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE job_runs ADD COLUMN {name} {definition}")

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def start(self, job_name: str, *, run_date: str | None = None,
              display_name: str = "", scheduled_at: str | None = None,
              input_dataset: str = "", output_dataset: str = "",
              parent_run_id: int | None = None) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO job_runs(
                    job_name,started_at,run_date,display_name,scheduled_at,
                    input_dataset,output_dataset,parent_run_id,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (job_name, now, run_date or now[:10], display_name, scheduled_at,
                 input_dataset, output_dataset, parent_run_id, now),
            )
            return int(cur.lastrowid)

    def finish(self, run_id: int, status: str = "success", result: Optional[dict] = None,
               error: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE job_runs SET finished_at=?,status=?,result=?,error=?,progress=?,updated_at=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), status,
                  json.dumps(result or {}, ensure_ascii=False, default=str), str(error)[:2000],
                  100 if status == "success" else 0,
                  datetime.now().isoformat(timespec="seconds"), run_id),
            )

    def update_progress(self, run_id: int, *, phase: str = "", progress: int = 0,
                        processed: int | None = None, total: int | None = None,
                        current_item: str = "", status: str | None = None,
                        error: str = "") -> None:
        fields = ["phase=?", "progress=?", "processed=?", "total=?", "current_item=?", "updated_at=?"]
        values = [phase, max(0, min(int(progress), 100)), processed, total, current_item,
                  datetime.now().isoformat(timespec="seconds")]
        if status is not None:
            fields.append("status=?")
            values.append(status)
        if error:
            fields.append("error=?")
            values.append(str(error)[:2000])
        values.append(int(run_id))
        with self._connect() as conn:
            conn.execute(f"UPDATE job_runs SET {', '.join(fields)} WHERE id=?", values)

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

    def get(self, run_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM job_runs WHERE id=?", (int(run_id),)).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["result"] = json.loads(item["result"])
        except Exception:
            item["result"] = {}
        return item

    def running(self, job_name: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM job_runs WHERE job_name=? AND status='running' ORDER BY id DESC LIMIT 1",
                (job_name,),
            ).fetchone()
        return dict(row) if row else None

    def ensure_daily_plan(self, run_date: str | None = None,
                          *, daily_time: str = "15:35") -> list[dict]:
        """Create the visible daily data-task plan without starting work."""
        run_date = run_date or date.today().isoformat()
        tasks = [
            ("daily_sync", "日线增量同步", daily_time, "数据源", "daily", ""),
            ("rebuild_indicators", "指标重建", "日线完成后", "daily", "indicators", "daily_sync"),
            ("rebuild_factors", "因子重建", "指标完成后", "indicators", "factors", "rebuild_indicators"),
            ("minute_snapshot", "观察池分钟采集", "09:30-11:30 / 13:00-15:00", "观察池", "minute", ""),
            ("online_snapshot", "在线快照兜底", "分钟采集未启用时", "观察池", "online", "minute_snapshot"),
        ]
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            for task_key, name, scheduled, source, output, blocked_by in tasks:
                conn.execute(
                    """INSERT INTO job_plan(
                       run_date,task_key,display_name,scheduled_at,input_dataset,
                       output_dataset,blocked_by,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(run_date,task_key) DO UPDATE SET updated_at=excluded.updated_at""",
                    (run_date, task_key, name, scheduled, source, output, blocked_by, now),
                )
        plans = self.daily_plan(run_date)
        runs = self.recent(200)
        now = datetime.now()
        for plan in plans:
            run = next((item for item in runs if item.get("job_name") == plan["task_key"] and str(item.get("started_at", ""))[:10] == run_date), None)
            if run:
                plan.update({"status": run.get("status"), "run_id": run.get("id"), "phase": run.get("phase", ""), "progress": run.get("progress", 0), "processed": run.get("processed"), "total": run.get("total"), "current_item": run.get("current_item", ""), "error": run.get("error", "")})
                continue
            if plan["task_key"] == "daily_sync":
                try:
                    scheduled = datetime.strptime(f"{run_date} {daily_time}", "%Y-%m-%d %H:%M")
                    plan["status"] = "waiting" if now < scheduled else "overdue"
                except ValueError:
                    plan["status"] = "scheduled"
            elif plan["task_key"] == "minute_snapshot":
                plan["status"] = "success" if any(item.get("job_name") == "minute_snapshot" and item.get("status") == "success" and str(item.get("started_at", ""))[:10] == run_date for item in runs) else "scheduled"
            elif plan["task_key"] == "online_snapshot":
                plan["status"] = "not_applicable" if any(item.get("job_name") == "minute_snapshot" and item.get("status") == "success" and str(item.get("started_at", ""))[:10] == run_date for item in runs) else "scheduled"
            else:
                upstream = next((item for item in plans if item["task_key"] == plan["blocked_by"]), None)
                plan["status"] = "scheduled" if upstream and upstream["status"] == "success" else "waiting_upstream"
        return plans

    def daily_plan(self, run_date: str | None = None) -> list[dict]:
        run_date = run_date or date.today().isoformat()
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM job_plan WHERE run_date=? ORDER BY id", (run_date,)).fetchall()
        return [dict(row) for row in rows]

    def link_plan_run(self, run_date: str, task_key: str, run_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE job_plan SET run_id=?,status='running',updated_at=? WHERE run_date=? AND task_key=?",
                         (run_id, datetime.now().isoformat(timespec="seconds"), run_date, task_key))
