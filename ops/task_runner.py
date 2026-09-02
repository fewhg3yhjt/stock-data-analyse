"""Common task execution wrapper for scheduled, manual and backfill runs."""

from __future__ import annotations

import threading
import logging
from pathlib import Path
from typing import Callable, Optional

from StockInvestmentTool.ops.job_runs import JobRunStore
from StockInvestmentTool.ops.task_center import TaskCenter

logger = logging.getLogger(__name__)


class TaskRunner:
    def __init__(self, task_db: Path | str, metadata_db: Path | str | None = None):
        self.task_db = Path(task_db)
        self.metadata_db = Path(metadata_db) if metadata_db else self.task_db
        self.center = TaskCenter(self.task_db, self.metadata_db)
        self.jobs = JobRunStore(self.task_db)

    def execute(self, task_key: str, worker: Callable[[int, dict], dict], *, request_id: str | None = None,
                trigger_type: str = "manual", period_start: str | None = None,
                period_end: str | None = None, parent_run_id: int | None = None,
                input_dataset: str = "", output_dataset: str = "") -> dict:
        if request_id:
            request = self.center.request(request_id)
            if request is None:
                raise ValueError("执行请求不存在")
        else:
            request_id = self.center.create_request(task_key, trigger_type,
                                                     period_start=period_start, period_end=period_end)
            request = self.center.request(request_id)
        run_id = self.jobs.start(
            task_key, run_date=(period_end or period_start),
            display_name=self._display_name(task_key), input_dataset=input_dataset,
            output_dataset=output_dataset, parent_run_id=parent_run_id,
            request_id=request_id, config_version=request["config_version"],
            trigger_type=request["trigger_type"], period_start=request["period_start"],
            period_end=request["period_end"], timezone="Asia/Shanghai",
        )
        self.center.update_request(request_id, "running")
        self.center.event(run_id, "任务开始", phase="start", event_type="start",
                          payload={"request_id": request_id, "task_key": task_key})
        # 任务锁：Scheduler 与手工/Retry 共用，防止同一任务并发执行
        lock_key = self.jobs.lock_key(task_key, period_start=request["period_start"],
                                      period_end=request["period_end"])
        if not self.jobs.acquire_lock(lock_key, run_id):
            self.jobs.finish(run_id, "skipped", {"reason": f"任务锁被占用: {lock_key}"})
            self.center.update_request(request_id, "skipped")
            return {"run_id": run_id, "request_id": request_id, "status": "skipped",
                    "result": {"reason": f"任务锁被占用: {lock_key}"}}
        stop_heartbeat = threading.Event()
        lease_seconds = 600
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, args=(lock_key, run_id, stop_heartbeat, lease_seconds),
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            result = worker(run_id, request) or {}
            status = self.jobs.result_status(result)
            self.jobs.finish(run_id, status, result)
            self.center.update_request(request_id, status)
            return {"run_id": run_id, "request_id": request_id, "status": status, "result": result}
        except Exception as exc:
            self.jobs.finish(run_id, "failed", error=str(exc))
            self.center.update_request(request_id, "failed")
            raise
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=1)
            try:
                self.jobs.release_lock(lock_key, run_id)
            except Exception as exc:  # noqa: BLE001
                # 任务结果已经落库，释放锁失败不能把成功任务改成异常；
                # 下次启动/定期回收会清理过期锁。
                logger.warning("任务锁释放失败，将由过期回收处理: %s", exc)

    def _heartbeat_loop(self, lock_key: str, run_id: int, stop: threading.Event,
                        lease_seconds: int = 600) -> None:
        interval = max(1.0, lease_seconds / 3)
        while not stop.wait(interval):
            try:
                self.jobs.heartbeat_lock(lock_key, run_id, lease_seconds=lease_seconds)
            except Exception:
                return

    def execute_pipeline(self, stages: list[tuple[str, Callable[[int, dict], dict]]], *,
                         trigger_type: str = "manual", period_start: str | None = None,
                         period_end: str | None = None, requested_by: str = "admin") -> dict:
        if not stages:
            raise ValueError("流水线至少需要一个任务")
        runs = []
        parent_run_id = None
        for task_key, worker in stages:
            request_id = self.center.create_request(
                task_key, trigger_type, period_start=period_start,
                period_end=period_end, requested_by=requested_by,
            )
            item = self.execute(task_key, worker, request_id=request_id,
                                parent_run_id=parent_run_id, input_dataset=task_key,
                                output_dataset=task_key)
            runs.append(item)
            parent_run_id = item["run_id"]
            if item["status"] in {"failed", "timeout"}:
                break
        return {"request_ids": [item["request_id"] for item in runs], "runs": runs,
                "status": runs[-1]["status"] if runs else "failed"}

    def _display_name(self, task_key: str) -> str:
        task = self.center.task(task_key)
        return task.get("display_name", task_key) if task else task_key
