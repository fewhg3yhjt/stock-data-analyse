from datetime import datetime

import pandas as pd

from ops.freshness import data_status
from ops.job_runs import JobRunStore


def test_plan_indicators_reads_daily_and_depends_on_daily(tmp_path):
    plan = {x["task_key"]: x for x in JobRunStore(tmp_path / "runs.db").ensure_daily_plan("2026-08-28")}
    assert plan["rebuild_indicators"]["input_dataset"] == "daily"
    assert plan["rebuild_indicators"]["blocked_by"] == "daily_sync"


def test_reclaim_data_running_respects_startup_boundary(tmp_path):
    store = JobRunStore(tmp_path / "runs.db")
    old = store.start("daily_sync")
    child = store.start("rebuild_indicators", parent_run_id=old)
    new = store.start("minute_snapshot")
    notify = store.start("notification_outbox")
    with store._connect() as conn:
        conn.execute("UPDATE job_runs SET started_at=? WHERE id IN (?,?)", ("2020-01-01T00:00:00", old, child))
    assert store.reclaim_data_running(before=datetime(2021, 1, 1)) == 2
    assert store.get(old)["status"] == store.get(child)["status"] == "failed"
    assert store.get(new)["status"] == store.get(notify)["status"] == "running"


def test_freshness_intraday_configuration_keeps_file_facts(tmp_path, monkeypatch):
    from warehouse.storage import Warehouse
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.minute_store().write(pd.DataFrame({"time": ["2026-08-28 10:00:00"], "code": ["sh600000"]}), day="2026-08-28")
    for minute, online in (("0", "1"), ("1", "1"), ("0", "0")):
        monkeypatch.setenv("WAREHOUSE_MINUTE_SNAPSHOT", minute)
        monkeypatch.setenv("WAREHOUSE_ONLINE_SNAPSHOT", online)
        items = {x.dataset: x for x in __import__("ops.freshness", fromlist=["dataset_statuses"]).dataset_statuses(warehouse=warehouse, now=datetime(2026, 8, 28, 10))}
        assert items["minute"].status == ("healthy" if minute == "1" else "disabled")
        assert items["minute"].rows == 1 and items["minute"].symbols == 1


def test_result_status_categories():
    assert JobRunStore.result_status({"rows": 0, "up_to_date": True}) == "skipped"
    assert JobRunStore.result_status({"rows": 3, "failed": ["x"]}) == "partial_success"
    assert JobRunStore.result_status({"rows": 0, "failed": ["x"]}) == "failed"


def test_daily_plan_keeps_long_running_data_jobs_visible(tmp_path):
    store = JobRunStore(tmp_path / "runs.db")
    daily_id = store.start("daily_sync", run_date="2026-08-28")
    store.update_progress(daily_id, phase="重建指标", progress=94, processed=5649, total=6868)
    indicator_id = store.start("rebuild_indicators", run_date="2026-08-28", parent_run_id=daily_id)
    store.update_progress(indicator_id, phase="计算指标", progress=82, processed=5649, total=6868)

    for _ in range(250):
        notification_id = store.start("notification_outbox", run_date="2026-08-28")
        store.finish(notification_id, "success", {"sent": 0})

    plans = {plan["task_key"]: plan for plan in store.ensure_daily_plan("2026-08-28")}
    assert plans["daily_sync"]["status"] == "running"
    assert plans["daily_sync"]["run_id"] == daily_id
    assert plans["rebuild_indicators"]["status"] == "running"
    assert plans["rebuild_indicators"]["run_id"] == indicator_id
