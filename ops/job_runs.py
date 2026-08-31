"""Durable scheduler job execution ledger."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional


class JobRunStore:
    DATA_JOBS = {"daily_sync", "rebuild_indicators", "minute_snapshot", "online_snapshot"}
    CATEGORIES = {"data", "notification", "business", "all"}

    @staticmethod
    def result_status(result: Optional[dict], *, empty_is_skipped: bool = True) -> str:
        """Use one conservative status policy for warehouse worker results.

        Priority:
        1. explicit `status` (success/failed/partial_success/skipped/cancelled
           and PASS/FAIL/WARNING quality outcomes)
        2. explicit `ok=False` -> failed
        3. explicit `publish_allowed=False` -> failed (never success)
        4. legacy inference from failed/rows/up_to_date
        """
        result = result or {}
        status = result.get("status")
        if status in {"success", "failed", "partial_success", "skipped", "cancelled"}:
            return status
        if result.get("ok") is False:
            return "failed"
        if result.get("publish_allowed") is False:
            return "failed"
        if status in {"PASS", "FAIL", "WARNING"}:
            if status == "FAIL":
                return "failed"
            if status == "WARNING":
                return "partial_success" if result.get("publish_allowed") else "failed"
            return "success"
        failed = result.get("failed") or result.get("failed_count", 0)
        produced = result.get("rows", result.get("added_rows", 0)) or 0
        if result.get("skipped") or (empty_is_skipped and result.get("up_to_date") and not failed):
            return "skipped"
        if failed and produced:
            return "partial_success"
        if failed or ("symbols" in result and not produced and not result.get("up_to_date")):
            return "failed"
        return "success" if produced or result.get("ok", True) else "failed"
    def __init__(self, db_path: Optional[Path | str] = None):
        if db_path is None:
            from StockInvestmentTool.ops.task_center import management_db_path
            db_path = management_db_path()  # 统一事实库：management.db
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
            conn.execute("""CREATE TABLE IF NOT EXISTS task_locks (
                lock_key TEXT PRIMARY KEY,
                owner_run_id INTEGER,
                acquired_at TEXT,
                heartbeat_at TEXT,
                expires_at TEXT
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
            "request_id": "TEXT", "config_version": "INTEGER", "trigger_type": "TEXT NOT NULL DEFAULT 'scheduled'",
            "period_start": "TEXT", "period_end": "TEXT", "timezone": "TEXT NOT NULL DEFAULT 'Asia/Shanghai'",
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
              parent_run_id: int | None = None, request_id: str | None = None,
              config_version: int | None = None, trigger_type: str = "scheduled",
              period_start: str | None = None, period_end: str | None = None,
              timezone: str = "Asia/Shanghai") -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO job_runs(
                    job_name,started_at,run_date,display_name,scheduled_at,
                    input_dataset,output_dataset,parent_run_id,updated_at,request_id,config_version,
                    trigger_type,period_start,period_end,timezone)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_name, now, run_date or now[:10], display_name, scheduled_at,
                  input_dataset, output_dataset, parent_run_id, now, request_id, config_version,
                  trigger_type, period_start, period_end, timezone),
            )
            return int(cur.lastrowid)

    def finish(self, run_id: int, status: str = "success", result: Optional[dict] = None,
               error: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE job_runs SET finished_at=?,status=?,result=?,error=?,
                   progress=CASE WHEN ? IN ('success','skipped') THEN 100 ELSE progress END,
                   updated_at=? WHERE id=?""",
                (datetime.now().isoformat(timespec="seconds"), status,
                  json.dumps(result or {}, ensure_ascii=False, default=str), str(error)[:2000],
                   status,
                   datetime.now().isoformat(timespec="seconds"), run_id),
            )
        try:
            from StockInvestmentTool.ops.task_center import TaskCenter
            TaskCenter(self.db_path).event(
                run_id, f"任务结束: {status}", level="ERROR" if status == "failed" else "INFO",
                phase="finished", event_type="finish", payload={"status": status, "error": error},
            )
        except Exception:
            pass

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
        try:
            from StockInvestmentTool.ops.task_center import TaskCenter
            TaskCenter(self.db_path).event(
                run_id, f"{phase or '任务执行'}: {progress}%",
                phase=phase, event_type="progress", processed=processed,
                total=total, current_item=current_item,
            )
        except Exception:
            pass

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

    @classmethod
    def category_for(cls, job_name: str) -> str:
        if job_name in cls.DATA_JOBS:
            return "data"
        if job_name == "notification_outbox" or job_name.startswith("notification"):
            return "notification"
        return "business"

    @staticmethod
    def _now_utc() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def acquire_lock(self, lock_key: str, run_id: int, lease_seconds: int = 600) -> bool:
        """原子获取任务锁（BEGIN IMMEDIATE + 条件写），避免重复执行。"""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        expires = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT owner_run_id, expires_at FROM task_locks WHERE lock_key=?", (lock_key,)).fetchone()
                if row and row["expires_at"] and row["expires_at"] > now:
                    return False
                conn.execute(
                    "INSERT INTO task_locks(lock_key,owner_run_id,acquired_at,heartbeat_at,expires_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(lock_key) DO UPDATE SET "
                    "owner_run_id=excluded.owner_run_id, acquired_at=excluded.acquired_at, "
                    "heartbeat_at=excluded.heartbeat_at, expires_at=excluded.expires_at",
                    (lock_key, run_id, now, now, expires),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def heartbeat_lock(self, lock_key: str, run_id: int, lease_seconds: int = 600) -> None:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        expires = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as conn:
            conn.execute("UPDATE task_locks SET heartbeat_at=?, expires_at=? WHERE lock_key=? AND owner_run_id=?",
                         (now, expires, lock_key, int(run_id)))

    def release_lock(self, lock_key: str, run_id: int | None = None) -> None:
        with self._connect() as conn:
            if run_id is not None:
                conn.execute("DELETE FROM task_locks WHERE lock_key=? AND owner_run_id=?", (lock_key, int(run_id)))
            else:
                conn.execute("DELETE FROM task_locks WHERE lock_key=?", (lock_key,))

    def recover_stale_locks(self) -> int:
        """回收超过租约的锁（进程重启遗留）。"""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM task_locks WHERE expires_at < ?", (now,))
            return cur.rowcount

    @staticmethod
    def lock_key(task_key: str, *, period_start: str | None = None,
                 period_end: str | None = None, partition: str | None = None) -> str:
        parts = ["task", task_key]
        if period_start:
            parts.append(f"start:{period_start}")
        if period_end:
            parts.append(f"end:{period_end}")
        if partition:
            parts.append(f"partition:{partition}")
        return ":".join(parts)

    def query(self, *, limit: int = 50, offset: int = 0,
              job_names: Optional[list[str]] = None, status: Optional[str] = None,
              parent: Optional[int] = None, category: str = "all") -> tuple[list[dict], int]:
        """Filter in SQL before pagination; returns (page, total)."""
        if category not in self.CATEGORIES:
            raise ValueError("非法任务类别")
        where, args = [], []
        if job_names:
            where.append("job_name IN (%s)" % ",".join("?" for _ in job_names)); args.extend(job_names)
        if status:
            where.append("status=?"); args.append(status)
        if parent is not None:
            where.append("parent_run_id=?"); args.append(int(parent))
        if category != "all":
            names = [n for n in self._all_job_names() if self.category_for(n) == category]
            if not names:
                return [], 0
            where.append("job_name IN (%s)" % ",".join("?" for _ in names)); args.extend(names)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM job_runs" + clause, args).fetchone()[0]
            rows = conn.execute("SELECT * FROM job_runs" + clause + " ORDER BY id DESC LIMIT ? OFFSET ?",
                                args + [max(1, int(limit)), max(0, int(offset))]).fetchall()
        return [self._decode(row) for row in rows], int(total)

    def _all_job_names(self) -> list[str]:
        with self._connect() as conn:
            return [row[0] for row in conn.execute("SELECT DISTINCT job_name FROM job_runs")]

    @staticmethod
    def _decode(row) -> dict:
        item = dict(row)
        try: item["result"] = json.loads(item.get("result") or "{}")
        except Exception: item["result"] = {}
        return item

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
        item["children"] = self.children(run_id)
        return item

    def children(self, run_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM job_runs WHERE parent_run_id=? ORDER BY id", (int(run_id),)).fetchall()
        return [self._decode(row) for row in rows]

    def reclaim_data_running(self, *, before: datetime | str) -> int:
        """Recover running data rows left by a process restart, once at startup."""
        boundary = before.isoformat(timespec="seconds") if isinstance(before, datetime) else str(before)
        names = tuple(self.DATA_JOBS)
        with self._connect() as conn:
            marks = ",".join("?" for _ in names)
            cur = conn.execute(f"""UPDATE job_runs SET status='failed', finished_at=?, updated_at=?,
                error='任务进程已结束，运行记录自动回收' WHERE status='running' AND started_at<?
                AND job_name IN ({marks})""",
                [datetime.now().isoformat(timespec="seconds")] * 2 + [boundary] + list(names))
        return cur.rowcount

    def running(self, job_name: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM job_runs WHERE job_name=? AND status='running' ORDER BY id DESC LIMIT 1",
                (job_name,),
            ).fetchone()
        return dict(row) if row else None

    def reclaim_stale(self, job_name: str, *, max_age_minutes: int = 30) -> int:
        """Mark abandoned running rows after a process/container restart."""
        cutoff = (datetime.now() - timedelta(minutes=max_age_minutes)).isoformat(timespec="seconds")
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE job_runs SET status='failed', finished_at=?, updated_at=?,
                   error='任务进程已结束，运行记录自动回收'
                   WHERE job_name=? AND status='running' AND started_at<?""",
                (now, now, job_name, cutoff),
            )
        return cur.rowcount

    def ensure_daily_plan(self, run_date: str | None = None,
                          *, daily_time: str = "15:35") -> list[dict]:
        """Create the visible daily data-task plan without starting work."""
        run_date = run_date or date.today().isoformat()
        tasks = [
            ("daily_sync", "日线增量同步", daily_time, "数据源", "daily", ""),
            ("rebuild_indicators", "指标重建", "日线完成后", "daily", "indicators", "daily_sync"),
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
                       ON CONFLICT(run_date,task_key) DO UPDATE SET
                       input_dataset=excluded.input_dataset, output_dataset=excluded.output_dataset,
                       blocked_by=excluded.blocked_by, scheduled_at=excluded.scheduled_at,
                       updated_at=excluded.updated_at""",
                    (run_date, task_key, name, scheduled, source, output, blocked_by, now),
                )
        plans = self.daily_plan(run_date)
        # High-frequency jobs can push a long-running data pipeline out of the
        # global recent window. Query only jobs relevant to this plan first.
        runs, _ = self.query(
            limit=1000,
            job_names={task[0] for task in tasks},
        )
        now = datetime.now()
        for plan in plans:
            # A long warehouse rebuild may cross midnight. Prefer a currently
            # running task regardless of its start date, then use the plan
            # date for completed historical runs.
            run = next((item for item in runs
                        if item.get("job_name") == plan["task_key"]
                        and item.get("status") == "running"), None)
            if run is None:
                run = next((item for item in runs
                            if item.get("job_name") == plan["task_key"]
                            and str(item.get("started_at", ""))[:10] == run_date), None)
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
