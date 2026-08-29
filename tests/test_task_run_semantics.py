import json
import sqlite3

from ops.management_db import ManagementDB
from ops.task_center_service import TaskCenterService


def test_task_state_separates_schedule_registration_and_history(tmp_path):
    path = tmp_path / "management.db"
    ManagementDB(path).seed_definitions()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO job_runs(job_name,started_at,status,result) VALUES('daily_sync','2026-08-28T10:00:00','failed',?)", (json.dumps({'failed':['sh600001']}),))
    task = next(item for item in TaskCenterService(path).tasks() if item['task_key']=='stock_daily_capture')
    assert task['configured'] is True
    assert task['enabled'] is False
    assert task['registered'] is False
    assert task['has_history'] is True
    assert task['latest_failure']['failed_items'] == ['sh600001']
