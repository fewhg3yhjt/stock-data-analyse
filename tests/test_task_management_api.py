import json
import sqlite3

import pytest

from StockInvestmentTool.ops.task_center import TaskCenter
from StockInvestmentTool.ops.task_center_service import TaskCenterService


def test_task_config_validation_and_activation(tmp_path):
    path = tmp_path / "management.db"
    center = TaskCenter(path)
    center.sync_definitions()
    task = center.task("indicators_build")
    config = json.loads(task["config_versions"][0]["config"])
    config["schedule"]["enabled"] = True
    version = center.save_task_config("indicators_build", config, activate=True)
    current = center.task("indicators_build")
    assert version == 2
    assert current["active_config_version"] == 2
    assert current["enabled"] == 1


def test_task_config_rejects_wrong_timezone(tmp_path):
    center = TaskCenter(tmp_path / "management.db")
    center.sync_definitions()
    config = json.loads(center.task("indicators_build")["config_versions"][0]["config"])
    config["schedule"]["timezone"] = "UTC"
    with pytest.raises(ValueError, match="北京时间"):
        center.save_task_config("indicators_build", config)


def test_task_service_can_toggle_enabled(tmp_path):
    path = tmp_path / "management.db"
    service = TaskCenterService(path)
    service.center.sync_definitions()
    assert service.set_enabled("money_flow_capture", True) == {
        "task_key": "money_flow_capture", "enabled": True,
    }
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT enabled FROM task_definitions WHERE task_key=?",
                            ("money_flow_capture",)).fetchone()[0] == 1


def test_task_execution_rejects_missing_universe(tmp_path):
    from StockInvestmentTool.ops.task_execution import execute_task
    center = TaskCenter(tmp_path / "management.db")
    center.sync_definitions()
    with pytest.raises(ValueError, match="证券范围"):
        execute_task(tmp_path / "management.db", "indicators_build", {})
