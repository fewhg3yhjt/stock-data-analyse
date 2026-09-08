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
from StockInvestmentTool.warehouse.asset_profiles import asset_type_for, select_symbols

logger = logging.getLogger(__name__)

DATA_TASK_TYPES = frozenset({
    "SOURCE_CAPTURE", "DATA_BUILD", "INDICATOR_BUILD", "DERIVED_BUILD",
    "QUALITY_CHECK", "DATA_PUBLISH", "FACTOR_BUILD",
})
DEFAULT_DATA_TASKS = (
    "stock_daily_capture", "stock_daily_build", "stock_daily_quality",
    "stock_daily_publish", "indicators_build", "factors_build",
    "industry_capture", "industry_membership_capture", "industry_daily_capture",
    "industry_features_build", "industry_rotation_build", "fundamentals_capture",
    "valuation_capture", "money_flow_capture", "financial_reports_capture",
    "financial_reports_build", "financial_reports_quality", "financial_reports_publish",
    "valuation_daily_build", "valuation_daily_quality", "valuation_daily_publish",
    "valuation_snapshot_capture", "valuation_snapshot_build", "valuation_snapshot_quality",
    "valuation_snapshot_publish",
)


def _worker_id() -> str:
    return os.getenv("DATA_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DataWorker:
    """领取并执行数据任务请求的单进程 Worker。"""

    def __init__(self, db_path: Path | str, *, task_keys: set[str] | None = None,
                 poll_interval: float = 2.0, task_timeout: float | None = None,
                 batch_size: int = 50):
        self.db_path = Path(db_path)
        self.task_keys = set(task_keys or ())
        self.poll_interval = max(0.1, float(poll_interval))
        self.task_timeout = task_timeout
        self.batch_size = max(1, int(batch_size))
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
        for key, default in (("symbols", []), ("input_versions", {}), ("request_payload", {})):
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
        payload.update(request.get("request_payload") or {})
        payload["period_start"] = request.get("period_start")
        payload["period_end"] = request.get("period_end")
        payload["symbols"] = request.get("symbols") or []
        payload["input_versions"] = request.get("input_versions") or {}
        if self.task_timeout is not None and not payload.get("task_timeout"):
            payload["task_timeout"] = self.task_timeout
        payload["requested_by"] = payload.get("requested_by") or f"data-worker:{self.worker_id}"
        try:
            result = self.execute_request(request, payload)
            if result.get("status") == "success":
                result["downstream_requests"] = self.enqueue_downstream(request, result)
        except Exception as exc:  # noqa: BLE001
            # execute_task normally owns Run finalization; this covers failures
            # before TaskRunner can create a Run after the request was claimed.
            self.center.update_request(request_id, "failed")
            logger.exception("数据任务执行失败: %s", request_id)
            return {"request_id": request_id, "status": "failed", "error": str(exc)}
        finally:
            self.heartbeat(status="idle")
        return result

    def execute_request(self, request: dict, payload: dict) -> dict:
        """Execute one request, batching mixed stock/ETF capture safely."""
        if request["task_key"] != "stock_daily_capture":
            return execute_task(self.db_path, request["task_key"], payload,
                                request_id=request["request_id"])

        symbols = payload.get("symbols") or self._active_symbols(request)
        groups = self._batch_symbols(symbols)
        if len(groups) <= 1:
            payload["symbols"] = groups[0][1] if groups else []
            payload["batch_index"] = 1 if groups else 0
            payload["batch_count"] = len(groups)
            return execute_task(self.db_path, request["task_key"], payload,
                                request_id=request["request_id"])

        results = []
        for index, (asset_type, batch) in enumerate(groups, 1):
            child_payload = dict(payload)
            child_payload.update({
                "symbols": batch,
                "asset_types": [asset_type],
                "batch_index": index,
                "batch_count": len(groups),
                "parent_request_id": request["request_id"],
            })
            child_id = self.center.create_request(
                request["task_key"], request.get("trigger_type") or "scheduled",
                period_start=request.get("period_start"), period_end=request.get("period_end"),
                symbols=batch, requested_by=f"data-worker:{self.worker_id}",
                input_versions=payload.get("input_versions") or {},
                request_payload=child_payload,
            )
            results.append(execute_task(self.db_path, request["task_key"], child_payload,
                                        request_id=child_id))

        statuses = [item.get("status") for item in results]
        failed = [item for item in results if item.get("status") not in {"success", "partial_success"}]
        source_batches = []
        asset_type_counts = {
            "stock": {"expected": 0, "success": 0, "failed": 0, "skipped": 0},
            "etf": {"expected": 0, "success": 0, "failed": 0, "skipped": 0},
        }
        for item in results:
            inner = item.get("result") or {}
            source_batches.extend(inner.get("source_batch_ids") or ([inner["source_batch_id"]] if inner.get("source_batch_id") else []))
            for kind, stats in (inner.get("coverage_by_type") or {}).items():
                target = asset_type_counts.setdefault(kind, {"expected": 0, "success": 0, "failed": 0, "skipped": 0})
                for key in target:
                    target[key] += int(stats.get(key, 0) or 0)
        status = "failed" if failed else ("partial_success" if "partial_success" in statuses else "success")
        self.center.update_request(request["request_id"], status)
        return {"run_id": None, "request_id": request["request_id"], "status": status,
                "result": {"batch_count": len(groups), "batches": results,
                            "source_batch_ids": source_batches,
                            "asset_type_counts": asset_type_counts}}

    def _active_symbols(self, request: dict) -> list[str]:
        """Resolve the configured active universe without mixing industries."""
        from StockInvestmentTool.warehouse.storage import Warehouse

        configured = self.center.task(request["task_key"]) or {}
        config = next((item for item in configured.get("config_versions", [])
                       if item.get("version") == configured.get("active_config_version")), {})
        try:
            config = json.loads(config.get("config") or "{}")
        except (TypeError, ValueError):
            config = {}
        allowed = (config.get("scope") or {}).get("asset_types") or ["stock", "etf"]
        warehouse = Warehouse(meta_db_path=self.db_path)
        symbols = [item["code"] for item in warehouse.list_instruments(asset_types=set(allowed))]
        return symbols

    def _batch_symbols(self, symbols: list[str]) -> list[tuple[str, list[str]]]:
        known = self.center.db_path
        from StockInvestmentTool.warehouse.storage import Warehouse

        types = Warehouse(meta_db_path=known).instrument_types()
        selected, _ = select_symbols(symbols, asset_types=["stock", "etf"], known_types=types)
        groups: dict[str, list[str]] = {"stock": [], "etf": []}
        for code in selected:
            groups.setdefault(asset_type_for(code, types), []).append(code)
        return [(kind, batch) for kind in ("stock", "etf")
                for batch_start in range(0, len(groups.get(kind, [])), self.batch_size)
                for batch in [groups[kind][batch_start:batch_start + self.batch_size]]]

    def enqueue_downstream(self, request: dict, result: dict) -> list[dict]:
        """Queue the next data stage only after a successful upstream stage."""
        if result.get("status") != "success":
            return []
        chain = {
            "stock_daily_capture": "stock_daily_build",
            "stock_daily_build": "stock_daily_quality",
            "stock_daily_quality": "stock_daily_publish",
            "stock_daily_publish": "indicators_build",
        }
        next_task = chain.get(request["task_key"])
        if not next_task:
            return []
        task = self.center.task(next_task)
        if not task or not task.get("enabled"):
            return []
        upstream = result.get("result") or result
        input_versions = upstream.get("output_versions") or upstream.get("versions") or {}
        input_batch_id = upstream.get("source_batch_id")
        payload = dict(request.get("request_payload") or {})
        payload.update({
            "input_versions": input_versions,
            "input_batch_id": input_batch_id,
            "parent_request_id": request["request_id"],
        })
        with self.center._connect() as conn:
            existing = conn.execute(
                """SELECT request_id,status FROM task_execution_requests
                   WHERE task_key=? AND period_start=? AND period_end=?
                     AND status IN ('requested','running','success')
                   ORDER BY created_at DESC LIMIT 1""",
                (next_task, request.get("period_start"), request.get("period_end")),
            ).fetchone()
        if existing:
            return [{"request_id": existing[0], "task_key": next_task,
                     "status": existing[1], "deduplicated": True}]
        request_id = self.center.create_request(
            next_task, request.get("trigger_type") or "scheduled",
            period_start=request.get("period_start"), period_end=request.get("period_end"),
            symbols=request.get("symbols") or [], requested_by=f"data-worker:{self.worker_id}",
            input_versions=input_versions, request_payload=payload,
        )
        return [{"request_id": request_id, "task_key": next_task,
                 "status": "requested", "deduplicated": False}]

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
    parser.add_argument("--tasks", default=os.getenv("DATA_WORKER_TASKS", ",".join(DEFAULT_DATA_TASKS)),
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
                        poll_interval=args.poll_interval, task_timeout=args.task_timeout,
                        batch_size=int(os.getenv("DATA_WORKER_BATCH_SIZE", "50")))
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
