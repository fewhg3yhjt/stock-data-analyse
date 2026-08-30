"""Scheduler job execution ledger tests."""

from __future__ import annotations

from ops.job_runs import JobRunStore


def test_job_run_lifecycle(tmp_path):
    store = JobRunStore(tmp_path / "runs.db")
    run_id = store.start("test_job")
    store.finish(run_id, "success", {"rows": 3})

    item = store.recent(1)[0]
    assert item["job_name"] == "test_job"
    assert item["status"] == "success"
    assert item["result"]["rows"] == 3


def test_daily_plan_is_chinese_and_has_dependencies(tmp_path):
    store = JobRunStore(tmp_path / "runs.db")
    plans = store.ensure_daily_plan("2026-08-27", daily_time="23:00")
    assert [item["display_name"] for item in plans[:2]] == ["日线增量同步", "指标重建"]
    assert plans[1]["blocked_by"] == "daily_sync"
