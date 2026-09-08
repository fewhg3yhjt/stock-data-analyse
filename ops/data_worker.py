"""独立数据平面 Worker 入口。

阶段 1 只新增执行角色，不切断 Web Scheduler 的旧生产路径。Worker 通过
management.db 原子领取显式配置的 data task request，并复用现有统一任务执行器。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

from StockInvestmentTool.ops.task_center import TaskCenter
from StockInvestmentTool.ops.task_execution import execute_task

logger = logging.getLogger(__name__)

DATA_TASK_TYPES = frozenset({
    "SOURCE_CAPTURE", "DATA_BUILD", "INDICATOR_BUILD", "DERIVED_BUILD",
    "QUALITY_CHECK", "DATA_PUBLISH", "FACTOR_BUILD",
})


def _worker_id() -> str:
    return os.getenv("DATA_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DataWorker:
    """领取并执行数据任务请求的单进程 Worker。"""

    def __init__(self, db_path: Path | str, *, task_keys: set[str] | None = None,
                 poll_interval: float = 2.0, task_timeout: float | None = None):
        self.db_path = Path(db_path)
        self.task_keys = set(task_keys or ())
        self.poll_interval = max(0.1, float(poll_interval))
        self.task_timeout = task_timeout
        self.worker_id = _worker_id()
        self.center = TaskCenter(self.db_path, self.db_path)
        self._ensure_worker_table()

    def _ensure_worker_table(self) -> None:
        with self.center._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS data_worker_heartbeats (
                   worker_id TEXT PRIMARY KEY, heartbeat_at TEXT NOT NULL,
                   process_id INTEGER NOT NULL, host TEXT NOT NULL,
                   status TEXT NOT NULL, current_request_id TEXT NOT NULL DEFAULT '',
                   updated_at TEXT NOT NULL
                )"""
            )

    def heartbeat(self, *, status: str = "idle", request_id: str = "") -> None:
        now = _now()
        with self.center._connect() as conn:
            conn.execute(
                """INSERT INTO data_worker_heartbeats
                   (worker_id,heartbeat_at,process_id,host,status,current_request_id,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(worker_id) DO UPDATE SET
                     heartbeat_at=excluded.heartbeat_at, process_id=excluded.process_id,
                     host=excluded.host, status=excluded.status,
                     current_request_id=excluded.current_request_id, updated_at=excluded.updated_at""",
                (self.worker_id, now, os.getpid(), socket.gethostname(), status, request_id, now),
            )

    def claim_next(self) -> dict | None:
        """Atomically claim one requested task from the configured allowlist."""
        if not self.task_keys:
            return None
        placeholders = ",".join("?" for _ in self.task_keys)
        with self.center._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"""SELECT * FROM task_execution_requests
                    JOIN task_definitions USING (task_key)
                    WHERE task_execution_requests.status='requested'
                      AND task_execution_requests.task_key IN ({placeholders})
                      AND task_definitions.task_type IN ({','.join('?' for _ in DATA_TASK_TYPES)})
                    ORDER BY created_at, request_id LIMIT 1""",
                tuple(sorted(self.task_keys)) + tuple(sorted(DATA_TASK_TYPES)),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            request_id = row["request_id"]
            updated = conn.execute(
                "UPDATE task_execution_requests SET status='running' "
                "WHERE request_id=? AND status='requested'",
                (request_id,),
            ).rowcount
            if updated != 1:
                conn.rollback()
                return None
            conn.commit()
        request = dict(row)
        for key, default in (("symbols", []), ("input_versions", {})):
            try:
                request[key] = json.loads(request.get(key) or json.dumps(default))
            except (TypeError, ValueError):
                request[key] = default
        return request

    def process_once(self) -> dict | None:
        self.heartbeat(status="idle")
        request = self.claim_next()
        if request is None:
            return None
        request_id = request["request_id"]
        self.heartbeat(status="running", request_id=request_id)
        payload = dict(request)
        if self.task_timeout is not None and not payload.get("task_timeout"):
            payload["task_timeout"] = self.task_timeout
        payload["requested_by"] = payload.get("requested_by") or f"data-worker:{self.worker_id}"
        try:
            result = execute_task(self.db_path, request["task_key"], payload, request_id=request_id)
        except Exception as exc:  # noqa: BLE001
            # execute_task normally owns Run finalization; this covers failures
            # before TaskRunner can create a Run after the request was claimed.
            self.center.update_request(request_id, "failed")
            logger.exception("数据任务执行失败: %s", request_id)
            return {"request_id": request_id, "status": "failed", "error": str(exc)}
        finally:
            self.heartbeat(status="idle")
        return result

    def run_loop(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self.heartbeat(status="idle")
        try:
            while not stop.is_set():
                self.process_once()
                stop.wait(self.poll_interval)
        finally:
            self.heartbeat(status="stopped")


def _parse_tasks(raw: str) -> set[str]:
    return {item.strip() for item in str(raw or "").split(",") if item.strip()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="StockInvestmentTool data worker")
    parser.add_argument("--db", type=Path, default=None, help="management.db 路径")
    parser.add_argument("--tasks", default=os.getenv("DATA_WORKER_TASKS", ""),
                        help="允许执行的 data task key，逗号分隔")
    parser.add_argument("--poll-interval", type=float,
                        default=float(os.getenv("DATA_WORKER_POLL_INTERVAL", "2")))
    parser.add_argument("--task-timeout", type=float, default=None)
    parser.add_argument("--once", action="store_true", help="只轮询一次")
    args = parser.parse_args(argv)
    if args.db is None:
        from StockInvestmentTool.ops.task_center import management_db_path
        args.db = management_db_path()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    worker = DataWorker(args.db, task_keys=_parse_tasks(args.tasks),
                        poll_interval=args.poll_interval, task_timeout=args.task_timeout)
    if args.once:
        worker.process_once()
        worker.heartbeat(status="stopped")
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    worker.run_loop(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
