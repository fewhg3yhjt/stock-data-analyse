from ops.task_runner import TaskRunner


def test_task_runner_uses_same_execution_contract_for_each_stage(tmp_path):
    runner = TaskRunner(tmp_path / "tasks.db")
    runner.center.sync_definitions()
    seen = []

    def worker(run_id, request):
        seen.append((run_id, request["task_key"], request["period_start"], request["period_end"]))
        runner.center.event(run_id, "阶段完成", phase="work", event_type="complete")
        return {"rows": 1}

    result = runner.execute_pipeline(
        [("stock_daily_build", worker), ("stock_daily_quality", worker)],
        trigger_type="backfill", period_start="2026-08-01", period_end="2026-08-28",
    )
    assert result["status"] == "success"
    assert result["request_ids"]
    assert seen == [
        (seen[0][0], "stock_daily_build", "2026-08-01", "2026-08-28"),
        (seen[1][0], "stock_daily_quality", "2026-08-01", "2026-08-28"),
    ]
    assert runner.jobs.get(result["runs"][1]["run_id"])["parent_run_id"] == result["runs"][0]["run_id"]


def test_task_runner_finishes_timeout_without_leaving_running(tmp_path, monkeypatch):
    runner = TaskRunner(tmp_path / "tasks.db")
    runner.center.sync_definitions()
    monkeypatch.setenv("EMAIL_TO", "")
    monkeypatch.setenv("BUSINESS_DB_PATH", str(tmp_path / "business.db"))

    result = runner.execute("stock_daily_build", lambda _run_id, _request: {"status": "timeout"})

    assert result["status"] == "timeout"
    assert runner.jobs.get(result["run_id"])["status"] == "timeout"


def test_task_runner_stops_pipeline_when_upstream_is_skipped(tmp_path):
    runner = TaskRunner(tmp_path / "tasks.db")
    runner.center.sync_definitions()

    def skipped_worker(_run_id, _request):
        return {"status": "skipped", "up_to_date": True}

    def downstream_worker(_run_id, _request):
        raise AssertionError("skipped upstream 后不应执行下游")

    result = runner.execute_pipeline(
        [("stock_daily_capture", skipped_worker), ("stock_daily_build", downstream_worker)],
        period_start="2026-09-04", period_end="2026-09-04",
    )

    assert result["status"] == "skipped"
    assert len(result["runs"]) == 1


def test_task_runner_failure_enqueues_outbox_event(tmp_path, monkeypatch):
    runner = TaskRunner(tmp_path / "tasks.db")
    runner.center.sync_definitions()
    monkeypatch.setenv("EMAIL_TO", "alerts@example.com")
    monkeypatch.setenv("BUSINESS_DB_PATH", str(tmp_path / "business.db"))
    result = runner.execute("stock_daily_build", lambda _run_id, _request: {"ok": False, "error": "boom"})

    assert result["status"] == "failed"
    from StockInvestmentTool.biz.notification import NotificationService
    events = NotificationService().list_events()
    assert any(event["event_type"] == "TASK_FAILED" and event["subject_id"] == str(result["run_id"])
               for event in events)


def test_task_runner_finish_is_idempotent_for_terminal_failure(tmp_path, monkeypatch):
    runner = TaskRunner(tmp_path / "tasks.db")
    runner.center.sync_definitions()
    monkeypatch.setenv("BUSINESS_DB_PATH", str(tmp_path / "business.db"))
    monkeypatch.setenv("EMAIL_TO", "alerts@example.com")
    run_id = runner.jobs.start("stock_daily_build")
    runner.jobs.finish(run_id, "timeout", error="deadline")
    runner.jobs.finish(run_id, "timeout", error="deadline again")
    from StockInvestmentTool.biz.notification import NotificationService
    service = NotificationService()
    assert len([e for e in service.list_events() if e["event_type"] == "TASK_FAILED"]) == 1
    assert service.repo.db.fetchone("SELECT COUNT(*) AS n FROM notification_deliveries")["n"] == 1


def test_task_failure_notification_is_idempotent_per_run(tmp_path, monkeypatch):
    runner = TaskRunner(tmp_path / "tasks.db")
    runner.center.sync_definitions()
    monkeypatch.setenv("EMAIL_TO", "alerts@example.com")
    monkeypatch.setenv("BUSINESS_DB_PATH", str(tmp_path / "business.db"))
    result = runner.execute("stock_daily_build", lambda _run_id, _request: {"ok": False, "error": "boom"})
    runner.jobs.finish(result["run_id"], "timeout", error="late timeout")
    from StockInvestmentTool.biz.notification import NotificationService
    service = NotificationService()
    events = [event for event in service.list_events()
              if event["event_type"] == "TASK_FAILED" and event["subject_id"] == str(result["run_id"])]
    assert len(events) == 1
