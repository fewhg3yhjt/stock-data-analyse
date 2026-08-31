# -*- coding: utf-8 -*-
"""阶段五：任务互斥、幂等与重启恢复。

覆盖：任务锁互斥、stale 回收时间格式（RFC3339）、启动回收遗留状态。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from StockInvestmentTool.biz.db import BusinessDB, now_utc
from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.tasks import BusinessTaskService, JOB_RUNNING, JOB_FAILED, register_task
from StockInvestmentTool.ops.job_runs import JobRunStore
from StockInvestmentTool.ops.task_center import TaskCenter


# ── 任务锁（ops 平面）─────────────────────────────

def test_job_run_lock_is_exclusive(tmp_path):
    store = JobRunStore(tmp_path / "jobs.db")
    key = store.lock_key("stock_daily_capture", period_start="2026-08-01", period_end="2026-08-28")
    assert store.acquire_lock(key, run_id=1)
    assert not store.acquire_lock(key, run_id=2)  # 未过期不可占用
    store.release_lock(key, run_id=1)
    assert store.acquire_lock(key, run_id=2)


def test_job_run_lock_key_includes_scope():
    key = JobRunStore.lock_key("stock_daily_capture", period_start="2026-08-01",
                               period_end="2026-08-28", partition="2026-08")
    assert key.startswith("task:stock_daily_capture")
    assert "start:2026-08-01" in key
    assert "partition:2026-08" in key


def test_job_run_lock_heartbeat_and_release(tmp_path):
    store = JobRunStore(tmp_path / "jobs.db")
    key = "task:indicators_build"
    store.acquire_lock(key, run_id=7)
    store.heartbeat_lock(key, run_id=7, lease_seconds=600)
    with store._connect() as conn:
        row = conn.execute("SELECT heartbeat_at, expires_at FROM task_locks WHERE lock_key=?", (key,)).fetchone()
    assert row["heartbeat_at"]
    assert row["expires_at"] > now_utc() or row["expires_at"] >= now_utc()
    store.release_lock(key, run_id=7)
    with store._connect() as conn:
        assert conn.execute("SELECT 1 FROM task_locks WHERE lock_key=?", (key,)).fetchone() is None


def test_recover_stale_locks(tmp_path):
    store = JobRunStore(tmp_path / "jobs.db")
    key = "task:stale"
    store.acquire_lock(key, run_id=1, lease_seconds=1)
    # 直接改 expires_at 为过去
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with store._connect() as conn:
        conn.execute("UPDATE task_locks SET expires_at=? WHERE lock_key=?", (past, key))
    assert store.recover_stale_locks() == 1
    with store._connect() as conn:
        assert conn.execute("SELECT 1 FROM task_locks WHERE lock_key=?", (key,)).fetchone() is None


# ── stale 回收时间格式（B1）────────────────────────

def test_recover_stale_runs_uses_rfc3339_cutoff(tmp_path):
    """heartbeat_at 为 RFC3339 UTC，旧格式 bug 恒假，这里验证新格式能正确回收。"""
    service = BusinessTaskService(BusinessRepository(BusinessDB(tmp_path / "task.db")))

    def run_ok(input_data):
        return {}

    register_task("screen.run", run_ok)
    # 造一个 heartbeat 在 10 分钟前（RFC3339）的 running run
    old_beat = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    service.repo.db.insert("business_job_runs", {
        "run_id": "stale2", "request_id": "r2", "task_key": "screen.run",
        "config_version": "", "trigger_type": "manual",
        "input_versions_json": "{}", "output_versions_json": "{}",
        "attempt": 1, "status": JOB_RUNNING, "started_at": old_beat,
        "heartbeat_at": old_beat, "finished_at": "", "error_code": "", "error_message": "",
    })
    assert service.recover_stale_runs() == 1
    row = service.repo.db.fetchone("SELECT * FROM business_job_runs WHERE run_id='stale2'")
    assert row["status"] == JOB_FAILED
    assert row["error_code"] == "PROCESS_RESTARTED"


def test_recent_rfc3339_heartbeat_not_reclaimed(tmp_path):
    """5 分钟内的 heartbeat 不应被回收。"""
    service = BusinessTaskService(BusinessRepository(BusinessDB(tmp_path / "task.db")))

    def run_ok(input_data):
        return {}

    register_task("screen.run", run_ok)
    fresh = now_utc()
    service.repo.db.insert("business_job_runs", {
        "run_id": "fresh1", "request_id": "r3", "task_key": "screen.run",
        "config_version": "", "trigger_type": "manual",
        "input_versions_json": "{}", "output_versions_json": "{}",
        "attempt": 1, "status": JOB_RUNNING, "started_at": fresh,
        "heartbeat_at": fresh, "finished_at": "", "error_code": "", "error_message": "",
    })
    assert service.recover_stale_runs() == 0


# ── 启动回收（工作项 7/8）────────────────────────

def test_recover_inflight_requests_marks_running_failed(tmp_path):
    center = TaskCenter(tmp_path / "runs.db")
    center.sync_definitions()
    request_id = center.create_request("indicators_build", "manual", period_start="2026-08-01", period_end="2026-08-28")
    center.update_request(request_id, "running")
    assert center.recover_inflight_requests() == 1
    assert center.request(request_id)["status"] == "failed"


def test_recover_inflight_publishing_marks_failed(tmp_path):
    from StockInvestmentTool.warehouse.pipeline_state import PipelineState, recover_inflight_publishing
    state = PipelineState(tmp_path / "meta.db")
    path = tmp_path / "cand.parquet"
    import pandas as pd
    pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"]}).to_parquet(path, index=False)
    version = state.create_version({
        "version_id": "v1", "partition": "2026-08", "path": path,
        "row_count": 1, "symbol_count": 1,
        "checksum": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
    }, source_batches=[])
    import sqlite3
    with sqlite3.connect(state.db_path) as conn:
        conn.execute("UPDATE dataset_versions SET publish_status='publishing' WHERE version_id=?", (version,))
    assert recover_inflight_publishing(state.db_path) == 1
    with sqlite3.connect(state.db_path) as conn:
        row = conn.execute("SELECT publish_status FROM dataset_versions WHERE version_id=?", (version,)).fetchone()
    assert row[0] == "publish_failed"


def test_recover_inflight_publishing_restores_matching_file(tmp_path):
    """正式文件 checksum 匹配新版本时，恢复补齐 dataset_current。"""
    import hashlib
    import sqlite3
    import pandas as pd
    from StockInvestmentTool.warehouse.pipeline_state import PipelineState, recover_inflight_publishing

    state = PipelineState(tmp_path / "meta.db")
    path = tmp_path / "cand.parquet"
    pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"]}).to_parquet(path, index=False)
    version = state.create_version({
        "version_id": "v1", "partition": "2026-08", "path": path,
        "row_count": 1, "symbol_count": 1,
        "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
    }, source_batches=[])
    with sqlite3.connect(state.db_path) as conn:
        conn.execute("UPDATE dataset_versions SET publish_status='publishing',published_path=? WHERE version_id=?",
                     (str(path), version))
    assert recover_inflight_publishing(state.db_path) == 1
    with sqlite3.connect(state.db_path) as conn:
        row = conn.execute("SELECT publish_status FROM dataset_versions WHERE version_id=?", (version,)).fetchone()
        current = conn.execute(
            "SELECT version_id FROM dataset_current WHERE dataset_name='stock_daily' AND partition_key='2026-08'"
        ).fetchone()
    assert row[0] == "published"
    assert current[0] == version