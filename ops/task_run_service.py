"""Task run detail, events, logs, artifacts and lineage aggregation."""

from __future__ import annotations

from pathlib import Path
import json

from StockInvestmentTool.ops.artifact_service import ArtifactService
from StockInvestmentTool.ops.job_runs import JobRunStore
from StockInvestmentTool.ops.task_center import TaskCenter
from StockInvestmentTool.ops.task_center_service import TaskCenterService


class TaskRunService:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.jobs = JobRunStore(self.db_path)
        self.center = TaskCenter(self.db_path, self.db_path)
        self.artifacts_service = ArtifactService(self.db_path)

    def detail(self, run_id: int) -> dict | None:
        item = self.jobs.get(run_id)
        if item is None:
            return None
        run = TaskCenterService._run_dto(item)
        request = self.center.request(item.get("request_id")) if item.get("request_id") else None
        if request:
            run["request"] = request
        payload = run.get("result") or {}
        if isinstance(payload, dict):
            run["input_versions"] = payload.get("input_versions", request.get("input_versions", {}) if request else {})
            run["output_versions"] = payload.get("output_versions", {})
        artifacts = self.center.artifacts(run_id=run_id)
        return {"run": run, "events": self.center.events(run_id), "artifacts": artifacts,
                "lineage": {artifact["artifact_id"]: self.center.lineage(artifact["artifact_id"]) for artifact in artifacts}}

    def logs(self, run_id: int, offset=0, limit=200, level: str | None = None, phase: str | None = None):
        path = self.db_path.parent / "task_logs" / f"{run_id}.log"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        if level:
            lines = [line for line in lines if f"[{level.upper()}]" in line]
        if phase:
            lines = [line for line in lines if f"[{phase}]" in line]
        offset = max(0, int(offset)); limit = max(1, min(int(limit), 1000))
        selected = lines[offset:offset + limit]
        return {"run_id": run_id, "lines": selected, "text": "\n".join(selected),
                "next_offset": offset + len(selected), "has_more": offset + len(selected) < len(lines)}
