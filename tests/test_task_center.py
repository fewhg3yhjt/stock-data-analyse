import json

import pandas as pd

from ops.task_center import TaskCenter, load_task_definitions


def test_task_definitions_are_loadable_and_seedable(tmp_path):
    center = TaskCenter(tmp_path / "runs.db")
    assert len(load_task_definitions()) >= 6
    assert center.sync_definitions() >= 6
    assert center.sync_metrics() >= 10
    assert {item["task_key"] for item in center.list_tasks()} >= {"stock_daily_capture", "indicators_build"}


def test_task_request_event_and_artifact_preview(tmp_path):
    center = TaskCenter(tmp_path / "runs.db")
    center.sync_definitions()
    request = center.create_request("indicators_build", "manual", period_start="2026-08-01", period_end="2026-08-28", symbols=["sh600000"])
    assert request.startswith("req_")
    center.event(7, "开始计算", phase="build", processed=1, total=2, current_item="sh600000")
    assert (tmp_path / "task_logs" / "7.log").exists()
    path = tmp_path / "2026-08.parquet"
    pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"], "ma20": [10.2]}).to_parquet(path, index=False)
    artifact = center.register_artifact(run_id=7, dataset_name="indicators", artifact_type="indicator_output", file_path=path)
    preview = center.preview_artifact(artifact, limit=1)
    assert preview["rows"][0]["code"] == "sh600000"
