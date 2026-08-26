from __future__ import annotations

from ops.job_runs import JobRunStore


def test_job_store_get_and_running(tmp_path):
    store = JobRunStore(tmp_path / "runs.db")
    run_id = store.start("daily_tasks")
    assert store.running("daily_tasks")["id"] == run_id
    assert store.get(run_id)["status"] == "running"
    store.finish(run_id, "success", {"ok": True})
    assert store.running("daily_tasks") is None
    assert store.get(run_id)["result"]["ok"] is True
