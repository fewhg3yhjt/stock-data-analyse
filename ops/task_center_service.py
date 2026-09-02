"""Page-oriented task-center queries over the unified management database."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from StockInvestmentTool.ops.task_center import TaskCenter
from StockInvestmentTool.ops.terminology import STATUS_LABELS, task_labels


class TaskCenterService:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.center = TaskCenter(self.db_path, self.db_path)

    def overview(self, date: str | None = None) -> dict:
        date = date or datetime.now().strftime("%Y-%m-%d")
        tasks = self.tasks()
        with self.center._connect() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) FROM job_runs WHERE started_at LIKE ? GROUP BY status",
                (date + "%",),
            ).fetchall()
        counts = {row[0]: int(row[1]) for row in rows}
        return {
            "date": date,
            "task_definition_count": len(tasks),
            "enabled_schedule_count": sum(1 for item in tasks if item["schedule"].get("enabled")),
            "disabled_schedule_count": sum(1 for item in tasks if not item["schedule"].get("enabled")),
            "run_counts": counts,
            "today_total": sum(counts.values()),
            "today_completed": counts.get("success", 0) + counts.get("partial_success", 0),
            "running": counts.get("running", 0),
            "failed": counts.get("failed", 0),
            "waiting": counts.get("requested", 0) + counts.get("scheduled", 0) + counts.get("waiting", 0),
            "configured": len(tasks),
            "registered": sum(1 for item in tasks if item.get("latest_run") or item.get("running_run")),
        }

    def tasks(self, stage: str | None = None, status: str | None = None) -> list[dict]:
        items = self.center.task_catalog()
        result = []
        for item in items:
            latest = self._run_dto(item.get("latest_run"))
            running = self._run_dto(item.get("running_run"))
            dto = {
                "task_key": item["task_key"],
                "display_name": item["display_name"],
                "stage": item["stage"],
                "stage_label": item.get("stage_label"),
                "task_type": item["task_type"],
                "task_type_label": item.get("task_type_label"),
                "active_config_version": item.get("active_config_version"),
                "schedule": item.get("schedule") or {},
                "scope": item.get("scope") or {},
                "execution": item.get("execution") or {},
                "policy": item.get("policy") or {},
                "config": item.get("config") or {},
                "input_datasets": (item.get("config") or {}).get("task", {}).get("input_datasets", []),
                "output_datasets": (item.get("config") or {}).get("task", {}).get("output_datasets", []),
                "producer_metrics": (item.get("config") or {}).get("task", {}).get("producer_metrics", []),
                "latest_run": latest,
                "running_run": running,
                "current_status": (running or latest or {}).get("status") or ("scheduled" if item.get("enabled") else "disabled"),
                "configured": True,
                # Scheduler registration is not implied by a run history record.
                # The management DB only knows configuration and execution facts.
                "registered": False,
                "enabled": bool(item.get("enabled")),
                "running": bool(running),
                "has_history": bool(latest),
                "latest_success": latest if latest and latest.get("status") in ("success", "partial_success") else None,
                 "latest_failure": latest if latest and latest.get("status") in ("failed", "timeout") else None,
            }
            if stage and dto["stage"] != stage:
                continue
            if status and dto["current_status"] != status:
                continue
            result.append(dto)
        return result

    def task(self, task_key: str) -> dict | None:
        task = next((item for item in self.tasks() if item["task_key"] == task_key), None)
        if task is None:
            return None
        with self.center._connect() as conn:
            task["config_versions"] = [dict(row) for row in conn.execute(
                "SELECT version,status,created_at,activated_at,checksum FROM task_config_versions WHERE task_key=? ORDER BY version DESC",
                (task_key,),
            ).fetchall()]
            task["recent_runs"] = [self._run_dto(dict(row)) for row in conn.execute(
                "SELECT * FROM job_runs WHERE job_name IN (%s) ORDER BY id DESC LIMIT 20" %
                ",".join("?" for _ in self._runtime_names(task_key)), self._runtime_names(task_key),
            ).fetchall()]
        return task

    def save_config(self, task_key: str, config: dict, *, activate: bool = False) -> dict:
        version = self.center.save_task_config(task_key, config, activate=activate)
        return {"task_key": task_key, "version": version, "active": activate}

    def set_enabled(self, task_key: str, enabled: bool) -> dict:
        self.center.set_task_enabled(task_key, enabled)
        return {"task_key": task_key, "enabled": bool(enabled)}

    def activate_config(self, task_key: str, version: int) -> dict:
        self.center.activate_task_config(task_key, version)
        return {"task_key": task_key, "version": int(version), "active": True}

    def runs(self, task_key: str, *, limit: int = 50, date: str | None = None) -> list[dict]:
        names = self._runtime_names(task_key)
        marks = ",".join("?" for _ in names)
        where = [f"job_name IN ({marks})"]
        args = list(names)
        if date:
            where.append("started_at LIKE ?")
            args.append(f"{date}%")
        with self.center._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM job_runs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                args + [max(1, min(int(limit), 200))],
            ).fetchall()
        return [self._run_dto(dict(row)) for row in rows]

    @staticmethod
    def _runtime_names(task_key):
        return {
            "stock_daily_capture": ["stock_daily_capture", "daily_sync"],
            "indicators_build": ["indicators_build", "rebuild_indicators"],
        }.get(task_key, [task_key])

    @staticmethod
    def _run_dto(run):
        if not run:
            return None
        item = dict(run)
        started = item.get("started_at")
        finished = item.get("finished_at")
        duration = None
        if started:
            try:
                end = datetime.fromisoformat(finished) if finished else datetime.now()
                duration = max(0, int((end - datetime.fromisoformat(started)).total_seconds()))
            except (TypeError, ValueError):
                duration = None
        item["run_id"] = item.get("id")
        item["status_label"] = STATUS_LABELS.get(item.get("status"), item.get("status"))
        item["duration_seconds"] = duration
        item["actual_period_start"] = item.get("period_start") or item.get("run_date")
        item["actual_period_end"] = item.get("period_end") or item.get("run_date")
        item["is_legacy"] = str(item.get("record_origin", "")).startswith("legacy")
        try:
            item["result"] = json.loads(item.get("result") or "{}") if isinstance(item.get("result"), str) else item.get("result", {})
        except (TypeError, ValueError):
            item["result"] = {}
        item["failed_items"] = item["result"].get("failed", []) if isinstance(item["result"], dict) else []
        item["failed_count"] = len(item["failed_items"]) if isinstance(item["failed_items"], list) else int(item["result"].get("failed_count", 0) or 0) if isinstance(item["result"], dict) else 0
        return item
