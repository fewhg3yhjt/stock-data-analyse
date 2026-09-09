"""独立数据平面 Worker 入口。

阶段 1 只新增执行角色，不切断 Web Scheduler 的旧生产路径。Worker 通过
management.db 原子领取显式配置的 data task request，并复用现有统一任务执行器。
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
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


class DataTaskTimeout(TimeoutError):
    """数据阶段超过 Worker 强制 deadline。"""


def _execute_task_child(db_path, task_key, payload, request_id, result_queue) -> None:
    """Run the existing task executor in a killable child process."""
    try:
        result_queue.put(("result", execute_task(db_path, task_key, payload, request_id=request_id)))
    except BaseException as exc:  # noqa: BLE001 - parent converts it to failed
        result_queue.put(("error", type(exc).__name__, str(exc)))


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
            if result is None:
                raise RuntimeError("数据任务未返回结果")
            if result.get("status") == "success":
                result["downstream_requests"] = self.enqueue_downstream(request, result)
        except DataTaskTimeout as exc:
            self.center.update_request(request_id, "timeout")
            logger.warning("数据任务超时: %s", request_id)
            return {"request_id": request_id, "status": "timeout", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            # execute_task normally owns Run finalization; this covers failures
            # before TaskRunner can create a Run after the request was claimed.
            self.center.update_request(request_id, "failed")
            logger.exception("数据任务执行失败: %s", request_id)
            return {"request_id": request_id, "status": "failed", "error": str(exc)}
        finally:
            self.heartbeat(status="idle")
        return result

    def execute_task_with_deadline(self, task_key: str, payload: dict,
                                   request_id: str, deadline: float | None = None) -> dict:
        """Execute a data stage in a killable process with a whole-stage deadline."""
        if deadline is None:
            return execute_task(self.db_path, task_key, payload, request_id=request_id)
        remaining = max(0.0, float(deadline) - time.monotonic())
        if remaining <= 0:
            raise DataTaskTimeout(f"数据任务已达到 deadline: {task_key}")
        context = multiprocessing.get_context("fork")
        result_queue = context.Queue()
        child = context.Process(
            target=_execute_task_child,
            args=(self.db_path, task_key, payload, request_id, result_queue),
            name=f"data-task-{task_key}",
        )
        child.start()
        child.join(remaining)
        if child.is_alive():
            child.terminate()
            child.join(5)
            self._close_abandoned_request(request_id, task_key,
                                           f"数据任务超过 whole-task deadline ({remaining:.1f}s)")
            raise DataTaskTimeout(f"数据任务超过 whole-task deadline: {task_key}")
        try:
            message = result_queue.get_nowait()
        except Exception:
            message = ("error", "ChildProcessExit", f"数据任务子进程退出，exitcode={child.exitcode}")
        finally:
            result_queue.close()
            result_queue.join_thread()
        if message[0] == "error":
            raise RuntimeError(message[2])
        return message[1]

    def _close_abandoned_request(self, request_id: str, task_key: str, reason: str) -> None:
        """Close all durable records left running after a killed child."""
        from StockInvestmentTool.ops.job_runs import JobRunStore
        from StockInvestmentTool.warehouse.source_batches import SourceBatchStore

        jobs = JobRunStore(self.db_path)
        with jobs._connect() as conn:
            run_ids = [row[0] for row in conn.execute(
                "SELECT id FROM job_runs WHERE request_id=? AND status='running'", (request_id,)
            ).fetchall()]
        for run_id in run_ids:
            jobs.finish(run_id, "timeout", result={"reason": reason, "task_key": task_key}, error=reason)
            with jobs._connect() as conn:
                lock_keys = [row[0] for row in conn.execute(
                    "SELECT lock_key FROM task_locks WHERE owner_run_id=?", (run_id,)
                ).fetchall()]
            for lock_key in lock_keys:
                jobs.release_lock(lock_key, run_id)
        with self.center._connect() as conn:
            has_batches = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_batches'"
            ).fetchone()
            batch_ids = [row[0] for row in conn.execute(
                "SELECT batch_id FROM source_batches WHERE job_run_id=? AND status='running'",
                (run_ids[0],) if run_ids else (-1,),
            ).fetchall()] if has_batches else []
        if batch_ids:
            store = SourceBatchStore(self.db_path)
            for batch_id in batch_ids:
                batch = store.get(batch_id) or {}
                store.finish(
                    batch_id, success_symbols=int(batch.get("success_symbols") or 0),
                    failed_symbols=max(0, int(batch.get("expected_symbols") or 0) - int(batch.get("success_symbols") or 0)),
                    skipped_symbols=int(batch.get("skipped_symbols") or 0), row_count=int(batch.get("row_count") or 0),
                    raw_path=batch.get("raw_path"), checksum=batch.get("checksum"),
                    file_size=batch.get("file_size"), status="failed", error_summary=reason,
                    failure_details=[reason],
                )
        self.center.update_request(request_id, "timeout")

    def execute_request(self, request: dict, payload: dict) -> dict:
        """Execute one request, batching mixed stock/ETF capture safely."""
        deadline = None
        if payload.get("task_timeout") is not None:
            deadline = time.monotonic() + max(0.0, float(payload["task_timeout"]))
        if request["task_key"] != "stock_daily_capture":
            return self.execute_task_with_deadline(
                request["task_key"], payload, request["request_id"], deadline,
            )

        symbols = payload.get("symbols") or self._active_symbols(request, payload)
        groups = self._batch_symbols(symbols)
        if len(groups) <= 1:
            payload["symbols"] = groups[0][1] if groups else []
            payload["batch_index"] = 1 if groups else 0
            payload["batch_count"] = len(groups)
            return self.execute_task_with_deadline(
                request["task_key"], payload,
                request["request_id"], deadline,
            )

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
            results.append(self.execute_task_with_deadline(
                request["task_key"], child_payload, child_id, deadline,
            ))

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

    def _active_symbols(self, request: dict, payload: dict | None = None) -> list[str]:
        """Resolve the configured active universe without mixing industries."""
        from StockInvestmentTool.warehouse.storage import Warehouse
        from StockInvestmentTool.warehouse.universe import UniverseStore

        configured = self.center.task(request["task_key"]) or {}
        config = next((item for item in configured.get("config_versions", [])
                       if item.get("version") == configured.get("active_config_version")), {})
        try:
            config = json.loads(config.get("config") or "{}")
        except (TypeError, ValueError):
            config = {}
        allowed = (config.get("scope") or {}).get("asset_types") or ["stock", "etf"]
        warehouse = Warehouse(meta_db_path=self.db_path)
        from StockInvestmentTool.warehouse.universe import UniverseStore
        snapshot_date = request.get("period_end") or request.get("period_start")
        universe_store = UniverseStore(self.db_path)
        catalog = warehouse.list_instruments(asset_types=set(allowed))
        universe = universe_store.resolve(
            snapshot_date=snapshot_date,
            fetch_full=self._fetch_full_universe,
            entity_types=set(allowed), fallback_to_catalog=catalog,
        ) if snapshot_date else None
        if universe and universe.get("authoritative"):
            self._upsert_universe_items(warehouse, universe["items"], snapshot_date)
        elif universe and payload is not None:
            payload["universe_source"] = universe.get("source")
            payload["universe_authoritative"] = bool(universe.get("authoritative"))
        if universe and universe.get("items"):
            if payload is not None:
                payload["universe_source"] = universe.get("source")
                payload["universe_authoritative"] = bool(universe.get("authoritative"))
                payload["universe_snapshot_date"] = universe.get("snapshot_date", snapshot_date)
            return [item["entity_id"] for item in universe["items"]]
        universe = universe_store.latest_snapshot(
            as_of=snapshot_date, entity_types=set(allowed),
        ) if snapshot_date else None
        if universe and universe.get("items"):
            return [item["entity_id"] for item in universe["items"]]
        return [item["code"] for item in warehouse.list_instruments(asset_types=set(allowed))
                if item.get("universe_status", "active") == "active"]

    @staticmethod
    def _fetch_full_universe(snapshot_date: str) -> list[dict]:
        """Fetch the explicitly requested day's full security universe."""
        from StockInvestmentTool.warehouse.collector import MarketCollector

        return MarketCollector().list_market(
            include_etf=True, include_index=False, day=snapshot_date,
        )

    @staticmethod
    def _upsert_universe_items(warehouse, items: list[dict], snapshot_date: str) -> None:
        """Persist authoritative universe metadata without retiring omissions."""
        from StockInvestmentTool.screener.board import detect_board

        rows = []
        for item in items:
            code = str(item.get("code") or item.get("entity_id") or "").lower().replace(".", "")
            asset_type = str(item.get("type") or item.get("entity_type") or "").lower()
            if not code or asset_type not in {"stock", "etf", "index"}:
                continue
            trade_status = str(item.get("tradeStatus") or item.get("trade_status") or "")
            active = trade_status.lower() in {"", "1", "active", "trading", "正常", "交易"}
            rows.append({
                "code": code, "name": item.get("name", ""), "type": asset_type,
                "board": detect_board(code) or ("" if asset_type == "stock" else asset_type),
                "trade_status": trade_status,
                "universe_status": "active" if active else "suspended",
                "first_seen_date": snapshot_date, "last_seen_date": snapshot_date,
                "last_source": "baostock",
            })
        warehouse.upsert_instruments(rows)

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
