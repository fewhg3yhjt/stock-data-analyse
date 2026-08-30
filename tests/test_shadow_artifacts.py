import pandas as pd

from ops.task_center import TaskCenter


def test_shadow_artifact_lineage_is_queryable(tmp_path):
    center = TaskCenter(tmp_path / "runs.db")
    paths = {}
    for name in ("raw", "candidate", "daily", "indicators"):
        path = tmp_path / f"{name}.parquet"
        pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"], "value": [1.0]}).to_parquet(path, index=False)
        paths[name] = center.register_artifact(run_id=1, dataset_name="stock_daily", artifact_type=name, file_path=path)
    for upstream, downstream in (("raw", "candidate"), ("candidate", "daily"), ("daily", "indicators")):
        center.link_lineage(paths[upstream], paths[downstream])
    assert len(center.lineage(paths["daily"])["upstream"]) == 1
    assert len(center.lineage(paths["daily"])["downstream"]) == 1
