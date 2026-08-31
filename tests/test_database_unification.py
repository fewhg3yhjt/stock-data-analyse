# -*- coding: utf-8 -*-
"""阶段六：统一数据库职责。

验证：JobRunStore 默认指向 management.db、Warehouse 显式注入元数据库、
ManagementDB 迁移登记机制。
"""

from __future__ import annotations

import sqlite3

from StockInvestmentTool.ops.job_runs import JobRunStore
from StockInvestmentTool.ops.management_db import ManagementDB
from StockInvestmentTool.ops.task_center import TaskCenter


def test_job_run_store_defaults_to_management_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MANAGEMENT_DB_PATH", str(tmp_path / "management.db"))
    store = JobRunStore()
    assert store.db_path == tmp_path / "management.db"
    assert store.db_path.exists()


def test_warehouse_accepts_explicit_meta_db(tmp_path):
    from StockInvestmentTool.warehouse.storage import Warehouse
    warehouse = Warehouse(tmp_path / "warehouse", meta_db_path=tmp_path / "management.db")
    assert warehouse.meta_db_path == tmp_path / "management.db"
    # 禁止运行时切换对象路径 —— 构造后 meta_db_path 固定
    assert warehouse.meta_db_path == tmp_path / "management.db"


def test_task_center_records_schema_migrations(tmp_path):
    center = TaskCenter(tmp_path / "runs.db")
    with center._connect() as conn:
        rows = conn.execute("SELECT migration_id, checksum_before, checksum_after FROM schema_migrations ORDER BY migration_id").fetchall()
    ids = [row[0] for row in rows]
    assert "v1_initial_task_center" in ids
    assert "v2_job_runs_managed" in ids
    for row in rows:
        assert row[1] == "ok"
        assert row[2] == "ok"


def test_task_center_migrations_idempotent(tmp_path):
    TaskCenter(tmp_path / "runs.db")
    TaskCenter(tmp_path / "runs.db")  # 二次初始化不重复登记
    with TaskCenter(tmp_path / "runs.db")._connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count == 2


def test_management_db_records_migrations(tmp_path):
    db = ManagementDB(tmp_path / "management.db")
    with db.connect() as conn:
        rows = conn.execute("SELECT migration_id, checksum_before, checksum_after FROM schema_migrations ORDER BY migration_id").fetchall()
    ids = [row[0] for row in rows]
    assert "v1_management_initial" in ids
    assert "v2_job_runs_legacy_import" in ids
    for row in rows:
        assert row[1] == "ok"
        assert row[2] == "ok"


def test_legacy_job_runs_import_marked_origin(tmp_path):
    legacy = tmp_path / "legacy_job_runs.db"
    store = JobRunStore(legacy)
    run_id = store.start("daily_sync", display_name="旧运行")
    store.finish(run_id, "success", {"rows": 1})

    target = tmp_path / "management.db"
    db = ManagementDB(target)
    result = db.copy_legacy_db(legacy, kind="job_runs")
    with sqlite3.connect(target) as conn:
        row = conn.execute("SELECT job_name, record_origin FROM job_runs WHERE id=?", (run_id,)).fetchone()
    assert row[0] == "daily_sync"
    assert row[1] == "legacy_job_runs"