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


def test_active_configs_reflect_definition_schedule(tmp_path):
    center = TaskCenter(tmp_path / "runs.db")
    center.sync_definitions()
    active = center.active_configs()
    capture = active["stock_daily_capture"]
    assert capture["task_key"] == "stock_daily_capture"
    assert capture["schedule"]["frequency"] == "trading_day"
    assert capture["schedule"]["timezone"] == "Asia/Shanghai"
    assert capture["enabled"] is True
    assert capture["version"] >= 1
    # 只有启用的任务在 active_configs 中体现 enabled
    for key, item in active.items():
        schedule = item["schedule"] or {}
        assert item["enabled"] == bool(item["enabled"] and schedule.get("enabled", False))


def test_active_configs_reflect_saved_and_activated_changes(tmp_path):
    center = TaskCenter(tmp_path / "runs.db")
    center.sync_definitions()
    original = center.active_configs()["money_flow_capture"]["schedule"]["time"]
    # 保存草稿不激活：active config 不变
    config = json.loads(center.task("money_flow_capture")["config_versions"][0]["config"])
    config["schedule"]["time"] = "09:05"
    center.save_task_config("money_flow_capture", config, activate=False)
    assert center.active_configs()["money_flow_capture"]["schedule"]["time"] == original
    # 激活后生效
    version = center.save_task_config("money_flow_capture", config, activate=True)
    assert center.active_configs()["money_flow_capture"]["schedule"]["time"] == "09:05"
    assert center.active_configs()["money_flow_capture"]["version"] == version
