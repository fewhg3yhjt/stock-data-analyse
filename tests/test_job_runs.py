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
