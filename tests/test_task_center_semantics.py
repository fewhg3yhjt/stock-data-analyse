from ops.task_center_service import TaskCenterService
from ops.management_db import ManagementDB


def test_task_overview_separates_configured_and_registered(tmp_path):
    path = tmp_path / "management.db"
    db = ManagementDB(path)
    db.seed_definitions()
    overview = TaskCenterService(path).overview()
    assert overview["configured"] == overview["task_definition_count"] == 6
    assert overview["registered"] == 0
