import json

from StockInvestmentTool.ops.task_center import TaskCenter


def test_validated_task_config_can_be_activated(tmp_path):
    center = TaskCenter(tmp_path / "management.db")
    center.sync_definitions()
    task = center.task("indicators_build")
    config = json.loads(task["config_versions"][0]["config"])
    version = center.save_task_config("indicators_build", config)
    center.activate_task_config("indicators_build", version)
    assert center.task("indicators_build")["active_config_version"] == version
