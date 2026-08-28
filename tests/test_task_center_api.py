import pandas as pd

from ops.job_runs import JobRunStore
from ops.task_center import TaskCenter


def test_job_progress_creates_events_and_log(tmp_path):
    store = JobRunStore(tmp_path / "runs.db")
    run_id = store.start("indicators_build")
    store.update_progress(run_id, phase="计算指标", progress=50, processed=1, total=2, current_item="sh600000")
    store.finish(run_id, "success", {"rows": 2})
    center = TaskCenter(tmp_path / "runs.db")
    events = center.events(run_id)
    assert [event["event_type"] for event in events] == ["progress", "finish"]
    assert "计算指标" in (tmp_path / "task_logs" / f"{run_id}.log").read_text(encoding="utf-8")


def test_artifact_preview_is_bounded_and_tracks_schema(tmp_path):
    center = TaskCenter(tmp_path / "runs.db")
    path = tmp_path / "sample.parquet"
    pd.DataFrame({"date": pd.date_range("2026-08-27", periods=3),
                  "code": ["sh600000", "sh600001", "sh600002"],
                  "close": [10.0, 11.0, 12.0]}).to_parquet(path, index=False)
    artifact_id = center.register_artifact(run_id=1, dataset_name="stock_daily",
                                           artifact_type="published_dataset", file_path=path)
    preview = center.preview_artifact(artifact_id, limit=2)
    assert preview["artifact"]["row_count"] == 3
    assert len(preview["rows"]) == 2
    assert preview["columns"] == ["date", "code", "close"]


def test_artifact_preview_rejects_path_outside_allowed_roots(tmp_path):
    center = TaskCenter(tmp_path / "runs.db")
    path = tmp_path.parent / "outside.parquet"
    pd.DataFrame({"code": ["sh600000"]}).to_parquet(path, index=False)
    artifact_id = center.register_artifact(file_path=path)
    try:
        center.preview_artifact(artifact_id)
    except PermissionError:
        pass
    else:
        raise AssertionError("outside artifact path should be rejected")
