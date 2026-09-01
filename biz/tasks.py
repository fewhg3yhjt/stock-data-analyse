# -*- coding: utf-8 -*-
"""业务任务框架：BusinessTaskDefinition / Request / JobRun / Lock / Event。

依据 docs/PLATFORM_RUNTIME_AND_OPERATIONS_DESIGN.md。
- 手工/定时/重试任务使用同一 Runner
- 任务互斥（数据库租约锁）、幂等、超时、重启恢复
- 长任务创建 Request/JobRun；轻量维护任务也写运行记录
- 真实交易不进入后台任务框架
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from StockInvestmentTool.biz.models import new_id, now_utc

logger = logging.getLogger(__name__)

# JobRun 状态
JOB_REQUESTED = "requested"
JOB_RUNNING = "running"
JOB_SUCCESS = "success"
JOB_PARTIAL = "partial_success"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

TERMINAL_STATUSES = {JOB_SUCCESS, JOB_PARTIAL, JOB_FAILED, JOB_CANCELLED}

ALLOWED_TRANSITIONS = {
    JOB_REQUESTED: {JOB_RUNNING, JOB_CANCELLED},
    JOB_RUNNING: {JOB_SUCCESS, JOB_PARTIAL, JOB_FAILED, JOB_CANCELLED},
    JOB_SUCCESS: set(),
    JOB_PARTIAL: set(),
    JOB_FAILED: set(),
    JOB_CANCELLED: set(),
}

# 注册的业务任务（task_key → handler）
TASK_HANDLERS: dict[str, Callable[[dict], dict]] = {}


def register_task(task_key: str, handler: Callable[[dict], dict]) -> None:
    TASK_HANDLERS[task_key] = handler


@dataclass
class BusinessTaskDefinition:
    task_key: str
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)
    result_schema: dict = field(default_factory=dict)
    enabled: bool = True


@dataclass
class BusinessExecutionRequest:
    request_id: str
    task_key: str
    trigger_type: str = "manual"   # scheduled/manual/backfill/retry
    config_version_id: str | None = None
    input: dict = field(default_factory=dict)
    requested_at: str = field(default_factory=now_utc)


@dataclass
class BusinessJobRun:
    run_id: str
    task_key: str
    lock_key: str = ""
    request_id: str | None = None
    trigger_type: str = "manual"
    config_version: str = ""
    input_versions: dict = field(default_factory=dict)
    output_versions: dict = field(default_factory=dict)
    attempt: int = 1
    status: str = JOB_REQUESTED
    started_at: str | None = None
    heartbeat_at: str | None = None
    finished_at: str | None = None
    error_code: str = ""
    error_message: str = ""


class TaskStateError(ValueError):
    pass


class BusinessTaskService:
    """业务任务服务：注册、请求、执行、状态机、锁。"""

    def __init__(self, repo=None):
        from StockInvestmentTool.biz.repo import BusinessRepository
        self.repo = repo or BusinessRepository()

    # ── 注册 ──────────────────────────────────────────────

    def register_definition(self, definition: BusinessTaskDefinition) -> None:
        self.repo.db.upsert("business_task_definitions", {
            "task_key": definition.task_key, "name": definition.name,
            "description": definition.description,
            "input_schema_json": _dumps(definition.input_schema),
            "result_schema_json": _dumps(definition.result_schema),
            "enabled": 1 if definition.enabled else 0,
            "created_at": now_utc(), "updated_at": now_utc(),
        }, "task_key")

    # ── 请求与执行 ────────────────────────────────────────

    def enqueue(self, task_key: str, *, trigger_type: str = "manual",
                config_version_id: str | None = None, input_data: dict | None = None) -> BusinessExecutionRequest:
        """创建持久化请求（HTTP 返回 202 前必须已持久化）。"""
        definition = self.repo.db.fetchone(
            "SELECT task_key,enabled FROM business_task_definitions WHERE task_key=?", (task_key,)
        )
        if definition and not definition["enabled"]:
            raise TaskStateError(f"业务任务已禁用: {task_key}")
        if not definition:
            self.register_definition(BusinessTaskDefinition(task_key=task_key, name=task_key))
        request = BusinessExecutionRequest(
            request_id=new_id("req"), task_key=task_key, trigger_type=trigger_type,
            config_version_id=config_version_id, input=input_data or {},
        )
        self.repo.db.insert("business_execution_requests", {
            "request_id": request.request_id, "task_key": request.task_key,
            "config_version_id": config_version_id or "", "trigger_type": trigger_type,
            "input_json": _dumps(request.input), "requested_at": request.requested_at,
        })
        return request

    def create_run_for_request(self, request_id: str) -> BusinessJobRun:
        """将已持久化请求转换为 requested JobRun，不执行 handler。"""
        request = self.repo.db.fetchone(
            "SELECT * FROM business_execution_requests WHERE request_id=?", (request_id,)
        )
        if not request:
            raise KeyError(f"unknown request: {request_id}")
        existing = self.repo.db.fetchone(
            "SELECT * FROM business_job_runs WHERE request_id=? ORDER BY rowid DESC LIMIT 1",
            (request_id,),
        )
        if existing:
            return self._row_to_job(existing)
        run = BusinessJobRun(
            run_id=new_id("job"), task_key=request["task_key"], request_id=request_id,
            lock_key=self.make_lock_key(request["task_key"], **_lock_parts(_loads(request["input_json"]))),
            trigger_type=request["trigger_type"], status=JOB_REQUESTED,
        )
        self.repo.db.insert("business_job_runs", {
            "run_id": run.run_id, "request_id": run.request_id, "task_key": run.task_key,
            "lock_key": run.lock_key,
            "config_version": "", "trigger_type": run.trigger_type,
            "input_versions_json": "{}", "output_versions_json": "{}",
            "attempt": 1, "status": JOB_REQUESTED, "started_at": "", "heartbeat_at": "",
            "finished_at": "", "error_code": "", "error_message": "",
        })
        return run

    def _claim_run(self, run_id: str | None = None) -> BusinessJobRun | None:
        """Atomically select and claim one requested run."""
        import datetime
        from datetime import timezone
        with self.repo.db.transaction() as conn:
            if run_id is None:
                row = conn.execute(
                    "SELECT * FROM business_job_runs WHERE status=? ORDER BY rowid LIMIT 1",
                    (JOB_REQUESTED,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM business_job_runs WHERE run_id=?", (run_id,)
                ).fetchone()
            if not row:
                if run_id is None:
                    return None
                raise KeyError(f"unknown run: {run_id}")
            if row["status"] != JOB_REQUESTED:
                raise TaskStateError(f"run is not requested: {row['status']}")
            run = self._row_to_job(row)
            lock_key = run.lock_key or self.make_lock_key(run.task_key)
            run.lock_key = lock_key
            now = now_utc()
            expires = datetime.datetime.now(timezone.utc) + datetime.timedelta(seconds=300)
            expires_text = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
            existing = conn.execute(
                "SELECT owner_run_id, expires_at FROM business_task_locks WHERE lock_key=?",
                (lock_key,),
            ).fetchone()
            if existing and existing["expires_at"] and existing["expires_at"] > now:
                raise TaskStateError(f"任务冲突: {run.task_key} 正在运行")
            conn.execute(
                "INSERT INTO business_task_locks(lock_key,owner_run_id,acquired_at,heartbeat_at,expires_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(lock_key) DO UPDATE SET owner_run_id=excluded.owner_run_id, "
                "acquired_at=excluded.acquired_at, heartbeat_at=excluded.heartbeat_at, expires_at=excluded.expires_at",
                (lock_key, run.run_id, now, now, expires_text),
            )
            conn.execute(
                "UPDATE business_job_runs SET status=?, started_at=?, heartbeat_at=? WHERE run_id=?",
                (JOB_RUNNING, now, now, run.run_id),
            )
            run.status = JOB_RUNNING
            run.started_at = now
            run.heartbeat_at = now
            return run

    def _execute_claimed(self, run: BusinessJobRun) -> BusinessJobRun:
        """Execute a run that has already been atomically claimed."""
        lock_key = run.lock_key
        stop_heartbeat = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(lock_key, run.run_id, stop_heartbeat),
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            request = self.repo.db.fetchone(
                "SELECT input_json FROM business_execution_requests WHERE request_id=?",
                (run.request_id,),
            )
            result = TASK_HANDLERS[run.task_key](_loads(request["input_json"]) if request else {})
            self._finish(run, JOB_SUCCESS, output_versions=result.get("output_versions", {}))
        except Exception as exc:  # noqa: BLE001
            logger.exception("business worker failed: %s", run.task_key)
            self._finish(run, JOB_FAILED, error_code="TASK_EXECUTION_FAILED", error_message=str(exc))
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=1)
            self.release_lock(lock_key, owner_run_id=run.run_id)
        return run

    def execute_run(self, run_id: str) -> BusinessJobRun:
        """Atomically claim and execute one requested JobRun."""
        run = self._claim_run(run_id)
        return self._execute_claimed(run)

    def run_next(self) -> BusinessJobRun | None:
        """领取最早 requested JobRun 并执行一个，供轻量 Worker 调用。

        竞争失败（另一 Worker 已领取同一 run）返回 None，不伪装成任务失败。
        """
        try:
            run = self._claim_run()
            if run is None:
                return None
            return self._execute_claimed(run)
        except TaskStateError as exc:
            logger.warning("run_next 竞争领取失败: %s", exc)
            return None

    def run(self, task_key: str, *, trigger_type: str = "manual",
            input_data: dict | None = None, request_id: str | None = None) -> BusinessJobRun:
        """同步执行一个任务（同一 Runner，幂等锁保护）。返回 JobRun。"""
        if task_key not in TASK_HANDLERS:
            raise KeyError(f"未注册业务任务: {task_key}")
        # 运行时注册表是执行器事实源；持久化定义必须同步存在，满足外键约束。
        if not self.repo.db.fetchone(
            "SELECT task_key FROM business_task_definitions WHERE task_key=?", (task_key,)
        ):
            self.register_definition(BusinessTaskDefinition(task_key=task_key, name=task_key))
        if request_id is None:
            request = self.enqueue(task_key, trigger_type=trigger_type, input_data=input_data)
            request_id = request.request_id
        run = self.create_run_for_request(request_id)
        return self.execute_run(run.run_id)

    def _finish(self, run: BusinessJobRun, status: str, *, output_versions: dict | None = None,
                error_code: str = "", error_message: str = "") -> None:
        run.status = status
        run.finished_at = now_utc()
        run.error_code = error_code
        run.error_message = error_message
        if output_versions:
            run.output_versions = output_versions
        self.repo.db.update("business_job_runs", {
            "status": status, "finished_at": run.finished_at,
            "output_versions_json": _dumps(run.output_versions),
            "error_code": error_code, "error_message": error_message,
        }, "run_id=?", (run.run_id,))

    def transition(self, run_id: str, to_status: str) -> None:
        """显式状态转换（供 Worker/恢复用）。"""
        row = self.repo.db.fetchone("SELECT * FROM business_job_runs WHERE run_id=?", (run_id,))
        if not row:
            raise KeyError(f"unknown run: {run_id}")
        cur = row["status"]
        if to_status not in ALLOWED_TRANSITIONS.get(cur, set()):
            raise TaskStateError(f"非法状态转换: {cur} -> {to_status}")
        self.repo.db.update("business_job_runs", {"status": to_status,
                                                  "finished_at": now_utc() if to_status in TERMINAL_STATUSES else ""},
                            "run_id=?", (run_id,))

    # ── 锁（数据库租约）───────────────────────────────────

    def acquire_lock(self, lock_key: str, run_id: str, lease_seconds: int = 300) -> bool:
        now = now_utc()
        import datetime
        from datetime import timezone
        expires = datetime.datetime.now(timezone.utc) + datetime.timedelta(seconds=lease_seconds)
        expires_text = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
        # BEGIN IMMEDIATE + 条件写，避免两个 Worker 同时看到空闲锁。
        with self.repo.db.transaction() as conn:
            row = conn.execute(
                "SELECT owner_run_id, expires_at FROM business_task_locks WHERE lock_key=?",
                (lock_key,),
            ).fetchone()
            if row and row["expires_at"] and row["expires_at"] > now:
                return False
            conn.execute(
                "INSERT INTO business_task_locks(lock_key,owner_run_id,acquired_at,heartbeat_at,expires_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(lock_key) DO UPDATE SET owner_run_id=excluded.owner_run_id, "
                "acquired_at=excluded.acquired_at, heartbeat_at=excluded.heartbeat_at, expires_at=excluded.expires_at",
                (lock_key, run_id, now, now, expires_text),
            )
            return True

    def release_lock(self, lock_key: str, owner_run_id: str | None = None) -> None:
        if owner_run_id:
            self.repo.db.execute(
                "DELETE FROM business_task_locks WHERE lock_key=? AND owner_run_id=?",
                (lock_key, owner_run_id),
            )
        else:
            self.repo.db.execute("DELETE FROM business_task_locks WHERE lock_key=?", (lock_key,))

    def heartbeat(self, lock_key: str, lease_seconds: int = 300,
                  owner_run_id: str | None = None) -> None:
        import datetime
        from datetime import timezone
        expires = datetime.datetime.now(timezone.utc) + datetime.timedelta(seconds=lease_seconds)
        now = now_utc()
        where = "lock_key=?"
        params = (lock_key,)
        if owner_run_id:
            where += " AND owner_run_id=?"
            params = (lock_key, owner_run_id)
        self.repo.db.update("business_task_locks",
                            {"heartbeat_at": now,
                             "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")},
                            where, params)
        if owner_run_id:
            self.repo.db.update("business_job_runs", {"heartbeat_at": now},
                                "run_id=? AND status=?", (owner_run_id, JOB_RUNNING))

    def _heartbeat_loop(self, lock_key: str, owner_run_id: str, stop: threading.Event,
                        lease_seconds: int = 300) -> None:
        interval = max(1.0, lease_seconds / 3)
        while not stop.wait(interval):
            row = self.repo.db.fetchone(
                "SELECT owner_run_id FROM business_task_locks WHERE lock_key=?", (lock_key,)
            )
            if not row or row["owner_run_id"] != owner_run_id:
                return
            self.heartbeat(lock_key, lease_seconds=lease_seconds, owner_run_id=owner_run_id)

    # ── 重启恢复 ──────────────────────────────────────────

    def recover_stale_runs(self) -> int:
        """回收 stale running 任务（PROCESS_RESTARTED）。

        heartbeat_at 使用 RFC3339 UTC（YYYY-MM-DDTHH:MM:SSZ），阈值必须同格式，
        否则 TEXT 字典序比较与 SQLite datetime()（空格格式）混用会恒假。
        """
        import datetime
        from datetime import timezone
        cutoff = (datetime.datetime.now(timezone.utc) - datetime.timedelta(minutes=5))
        cutoff_text = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.repo.db.fetchall(
            "SELECT * FROM business_job_runs WHERE status='running' "
            "AND heartbeat_at IS NOT NULL AND heartbeat_at != '' "
            "AND heartbeat_at < ?", (cutoff_text,)
        )
        count = 0
        for row in rows:
            self.repo.db.update("business_job_runs", {
                "status": JOB_FAILED, "error_code": "PROCESS_RESTARTED",
                "error_message": "进程重启，运行被回收", "finished_at": now_utc(),
            }, "run_id=?", (row["run_id"],))
            count += 1
        return count

    def list_runs(self, task_key: str | None = None, limit: int = 100) -> list[dict]:
        if task_key:
            rows = self.repo.db.fetchall(
                "SELECT * FROM business_job_runs WHERE task_key=? ORDER BY rowid DESC LIMIT ?",
                (task_key, limit))
        else:
            rows = self.repo.db.fetchall(
                "SELECT * FROM business_job_runs ORDER BY rowid DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    @staticmethod
    def make_lock_key(task_key: str, *, period: str = "", partition: str = "",
                      write_group: str = "") -> str:
        return f"task:{task_key}|period:{period}|partition:{partition}|write:{write_group}"

    @staticmethod
    def _row_to_job(row) -> BusinessJobRun:
        return BusinessJobRun(
            run_id=row["run_id"], task_key=row["task_key"], request_id=row["request_id"],
            lock_key=row["lock_key"] if "lock_key" in row.keys() else "",
            trigger_type=row["trigger_type"], config_version=row["config_version"],
            input_versions=_loads(row["input_versions_json"]),
            output_versions=_loads(row["output_versions_json"]), attempt=row["attempt"],
            status=row["status"], started_at=row["started_at"] or None,
            heartbeat_at=row["heartbeat_at"] or None, finished_at=row["finished_at"] or None,
            error_code=row["error_code"], error_message=row["error_message"],
        )


def _dumps(value: Any) -> str:
    import json
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None) -> dict:
    import json
    try:
        loaded = json.loads(value or "{}")
        return loaded if isinstance(loaded, dict) else {}
    except (TypeError, ValueError):
        return {}


def _lock_parts(input_data: dict) -> dict[str, str]:
    return {
        "period": str(input_data.get("period", "")),
        "partition": str(input_data.get("partition", "")),
        "write_group": str(input_data.get("write_group", "")),
    }
