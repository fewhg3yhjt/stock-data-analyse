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
