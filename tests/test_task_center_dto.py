import sqlite3

from ops.task_center import TaskCenter


def test_task_catalog_joins_definition_schedule_and_legacy_runtime(tmp_path):
    center = TaskCenter(tmp_path / "management.db")
    center.sync_definitions()
    with center._connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_name TEXT NOT NULL, started_at TEXT NOT NULL,
            finished_at TEXT, status TEXT NOT NULL DEFAULT 'running', result TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '', run_date TEXT, display_name TEXT NOT NULL DEFAULT '',
            scheduled_at TEXT, phase TEXT NOT NULL DEFAULT '', progress INTEGER NOT NULL DEFAULT 0,
            processed INTEGER, total INTEGER, current_item TEXT NOT NULL DEFAULT '', input_dataset TEXT NOT NULL DEFAULT '',
            output_dataset TEXT NOT NULL DEFAULT '', parent_run_id INTEGER, updated_at TEXT, request_id TEXT,
            config_version INTEGER, trigger_type TEXT NOT NULL DEFAULT 'scheduled', period_start TEXT,
            period_end TEXT, timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai', record_origin TEXT NOT NULL DEFAULT 'new')""")
        conn.execute("INSERT INTO job_runs(job_name,started_at,status,progress,period_start,period_end) VALUES('rebuild_indicators','2026-08-29T10:00:00','running',72,'2026-08-01','2026-08-28')")
    task = next(item for item in center.task_catalog() if item["task_key"] == "indicators_build")
    assert task["schedule"]["frequency"] == "after_upstream"
    assert task["running_run"]["progress"] == 72
    assert task["running_run"]["period_start"] == "2026-08-01"
