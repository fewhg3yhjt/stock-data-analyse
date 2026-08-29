import sqlite3

import pandas as pd

from ops.artifact_service import ArtifactService
from ops.data_center_service import DataCenterService
from ops.management_db import ManagementDB
from ops.task_center import TaskCenter
from ops.task_center_service import TaskCenterService
from ops.task_run_service import TaskRunService


def seeded_db(tmp_path):
    path = tmp_path / "management.db"
    db = ManagementDB(path)
    db.seed_definitions()
    return path


def test_data_center_service_separates_overview_and_catalog(tmp_path):
    path = seeded_db(tmp_path)
    service = DataCenterService(path)
    overview = service.overview()
    assets = service.assets()
    assert len(assets) >= len(overview["core_assets"])
    assert {item["asset_key"] for item in overview["core_assets"]} <= set(DataCenterService.CORE_KEYS)
    assert "task_summary" in overview and "pipeline" in overview


def test_task_center_service_joins_configuration_and_runtime(tmp_path):
    path = seeded_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO job_runs(job_name,started_at,status,progress,period_start,period_end) VALUES('rebuild_indicators','2026-08-29T10:00:00','running',72,'2026-08-01','2026-08-28')")
    task = TaskCenterService(path).task("indicators_build")
    assert task["schedule"]["frequency"] == "after_upstream"
    assert task["running_run"]["progress"] == 72
    assert task["running_run"]["actual_period_start"] == "2026-08-01"


def test_task_run_and_artifact_services_return_details(tmp_path):
    path = seeded_db(tmp_path)
    center = TaskCenter(path, path)
    with sqlite3.connect(path) as conn:
        run_id = conn.execute("INSERT INTO job_runs(job_name,started_at,status) VALUES('indicators_build','2026-08-29T10:00:00','success')").lastrowid
    data_path = tmp_path / "indicator.parquet"
    pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"], "MA20": [10.2]}).to_parquet(data_path, index=False)
    artifact_id = center.register_artifact(run_id=run_id, dataset_name="indicators", artifact_type="indicator_output", file_path=data_path)
    detail = TaskRunService(path).detail(run_id)
    assert detail["artifacts"][0]["artifact_id"] == artifact_id
    summary = ArtifactService(path).summary(artifact_id)
    assert summary["row_count"] == 1
    assert summary["columns"][0]["name"] == "date"
