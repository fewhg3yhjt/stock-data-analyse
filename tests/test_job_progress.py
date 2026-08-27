from __future__ import annotations

from ops.job_runs import JobRunStore


def test_job_progress_is_persisted(tmp_path):
    store = JobRunStore(tmp_path / "runs.db")
    run_id = store.start("daily_sync", display_name="日线增量同步")
    store.update_progress(run_id, phase="读取日线", progress=42,
                          processed=42, total=100, current_item="sh600900")
    item = store.get(run_id)
    assert item["phase"] == "读取日线"
    assert item["progress"] == 42
    assert item["processed"] == 42
    assert item["current_item"] == "sh600900"
