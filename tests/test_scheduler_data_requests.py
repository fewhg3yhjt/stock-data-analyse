from pathlib import Path

from ops.task_center import TaskCenter


def test_scheduler_enqueues_data_request_without_executing(tmp_path, monkeypatch):
    from StockInvestmentTool.web import scheduler

    db = tmp_path / "management.db"
    center = TaskCenter(db, db)
    center.sync_definitions()
    monkeypatch.setattr(scheduler, "management_db_path", lambda: db, raising=False)
    monkeypatch.setattr("StockInvestmentTool.ops.task_center.management_db_path", lambda: db)

    called = []
    monkeypatch.setattr("StockInvestmentTool.ops.task_execution.execute_task", lambda *args: called.append(args))
    result = scheduler._enqueue_data_request(
        "stock_daily_capture", period_start="2026-09-08", period_end="2026-09-08",
        task_timeout=1800,
    )
    duplicate = scheduler._enqueue_data_request(
        "stock_daily_capture", period_start="2026-09-08", period_end="2026-09-08",
        task_timeout=1800,
    )

    assert result["status"] == "requested"
    assert duplicate["deduplicated"] is True
    assert called == []
    request = center.request(result["request_id"])
    assert request["request_payload"]["task_timeout"] == 1800
