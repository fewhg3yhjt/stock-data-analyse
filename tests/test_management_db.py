import sqlite3

from ops.management_db import ManagementDB, MANAGEMENT_SCHEMA_VERSION


def test_management_db_has_unified_schema_and_legacy_origin(tmp_path):
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as conn:
        conn.execute("CREATE TABLE task_definitions (task_key TEXT PRIMARY KEY, display_name TEXT NOT NULL, stage TEXT NOT NULL, task_type TEXT NOT NULL, enabled INTEGER NOT NULL, active_config_version INTEGER, updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO task_definitions VALUES ('old_task','旧任务','CAPTURE','SOURCE_CAPTURE',0,1,'2026-08-28')")
    db = ManagementDB(tmp_path / "management.db")
    result = db.migrate_from(job_db=legacy)
    assert result["job_runs"]["tables"]["task_definitions"] == 1
    assert db.counts()["task_definitions"] == 10
    with db.connect() as conn:
        assert conn.execute("SELECT value FROM management_meta WHERE key='schema_version'").fetchone()[0] == MANAGEMENT_SCHEMA_VERSION
        assert conn.execute("SELECT display_name FROM task_definitions WHERE task_key='old_task'").fetchone()[0] == "旧任务"


def test_legacy_running_jobs_are_reclaimed_during_migration(tmp_path):
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as conn:
        conn.execute("CREATE TABLE job_runs (id INTEGER PRIMARY KEY, job_name TEXT, started_at TEXT, finished_at TEXT, status TEXT, result TEXT, error TEXT)")
        conn.execute("INSERT INTO job_runs VALUES (1,'daily_tasks','2020-01-01',NULL,'running','{}','')")
    db = ManagementDB(tmp_path / "management.db")
    db.migrate_from(job_db=legacy)
    with db.connect() as conn:
        status, error = conn.execute("SELECT status,error FROM job_runs WHERE id=1").fetchone()
    assert status == "failed"
    assert "迁移时回收" in error


def test_management_db_migration_does_not_modify_legacy(tmp_path):
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as conn:
        conn.execute("CREATE TABLE dataset_registry (dataset_name TEXT PRIMARY KEY, display_name TEXT NOT NULL, description TEXT NOT NULL, grain TEXT NOT NULL, primary_keys TEXT NOT NULL, partition_type TEXT NOT NULL, storage_path TEXT NOT NULL, update_frequency TEXT, enabled INTEGER NOT NULL, schema_version TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO dataset_registry VALUES ('stock_daily','日线','desc','day','[]','month','daily','daily',1,'v1','a','b')")
    before = legacy.read_bytes()
    ManagementDB(tmp_path / "management.db").migrate_from(warehouse_db=legacy)
    assert legacy.read_bytes() == before


def test_management_db_seeds_yaml_definitions(tmp_path):
    db = ManagementDB(tmp_path / "management.db")
    seeded = db.seed_definitions()
    assert seeded["tasks"] >= 6
    assert seeded["metrics"] >= 10
    assert seeded["datasets"] == 6
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM dataset_registry").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM task_definitions").fetchone()[0] >= 6
        assert conn.execute("SELECT COUNT(*) FROM metric_definitions").fetchone()[0] >= 10


def test_management_db_can_be_used_as_single_task_and_data_source(tmp_path, monkeypatch):
    from ops.task_center import TaskCenter
    db_path = tmp_path / "management.db"
    db = ManagementDB(db_path)
    db.seed_definitions()
    center = TaskCenter(db_path, db_path)
    center.create_request("indicators_build", "manual", period_start="2026-08-01", period_end="2026-08-28")
    assert center.request(center._connect().execute("SELECT request_id FROM task_execution_requests").fetchone()[0])["task_key"] == "indicators_build"
