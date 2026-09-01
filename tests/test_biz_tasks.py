# -*- coding: utf-8 -*-
"""biz 包单元测试：业务任务框架。"""

import threading

import pytest

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.tasks import (
    BusinessTaskDefinition,
    BusinessTaskService,
    JOB_FAILED,
    JOB_RUNNING,
    JOB_SUCCESS,
    TaskStateError,
    register_task,
)


@pytest.fixture
def svc(tmp_path):
    return BusinessTaskService(BusinessRepository(BusinessDB(tmp_path / "task.db")))


def run_ok(input_data: dict) -> dict:
    return {"output_versions": {"screen_run_id": "run_ok"}}


def run_bad(input_data: dict) -> dict:
    raise RuntimeError("boom")


class TestTaskService:
    def test_register_and_enqueue(self, svc):
        svc.register_definition(BusinessTaskDefinition(task_key="screen.run", name="选股"))
        request = svc.enqueue("screen.run", input_data={"screen_id": "sc1"})
        row = svc.repo.db.fetchone(
            "SELECT * FROM business_execution_requests WHERE request_id=?", (request.request_id,))
        assert row["task_key"] == "screen.run"

    def test_run_success(self, svc):
        register_task("screen.run", run_ok)
        run = svc.run("screen.run", input_data={"screen_id": "sc1"})
        assert run.status == JOB_SUCCESS
        assert run.output_versions == {"screen_run_id": "run_ok"}
        request = svc.repo.db.fetchone(
            "SELECT request_id FROM business_execution_requests WHERE request_id=?",
            (run.request_id,),
        )
        assert request is not None

    def test_run_failure(self, svc):
        register_task("research.run", run_bad)
        run = svc.run("research.run", input_data={})
        assert run.status == JOB_FAILED
        assert run.error_code == "TASK_EXECUTION_FAILED"

    def test_unknown_task_raises(self, svc):
        with pytest.raises(KeyError):
            svc.run("no_such_task", input_data={})

    def test_lock_exclusive(self, svc):
        assert svc.acquire_lock("task:simulation.run", "run1")
        assert not svc.acquire_lock("task:simulation.run", "run2")  # 未过期不可占用
        svc.release_lock("task:simulation.run")
        assert svc.acquire_lock("task:simulation.run", "run2")

    def test_recover_stale_runs(self, svc):
        register_task("screen.run", run_ok)
        svc.run("screen.run", input_data={})
        # 手动造一条 running
        svc.repo.db.insert("business_job_runs", {
            "run_id": "stale1", "request_id": "r1", "task_key": "screen.run",
            "config_version": "", "trigger_type": "manual",
            "input_versions_json": "{}", "output_versions_json": "{}",
            "attempt": 1, "status": JOB_RUNNING, "started_at": "2026-08-30T00:00:00Z",
            "heartbeat_at": "2026-08-30T00:00:00Z", "finished_at": "", "error_code": "", "error_message": "",
        })
        n = svc.recover_stale_runs()
        assert n >= 1
        row = svc.repo.db.fetchone("SELECT * FROM business_job_runs WHERE run_id='stale1'")
        assert row["status"] == JOB_FAILED
        assert row["error_code"] == "PROCESS_RESTARTED"

    def test_illegal_transition_raises(self, svc):
        register_task("screen.run", run_ok)
        run = svc.run("screen.run", input_data={})
        with pytest.raises(TaskStateError):
            svc.transition(run.run_id, JOB_RUNNING)  # success -> running 非法

    def test_list_runs(self, svc):
        register_task("screen.run", run_ok)
        svc.run("screen.run", input_data={})
        runs = svc.list_runs("screen.run")
        assert len(runs) >= 1

    def test_request_and_worker_run_are_separate(self, svc):
        register_task("health.reconcile", run_ok)
        svc.register_definition(__import__(
            "StockInvestmentTool.biz.tasks", fromlist=["BusinessTaskDefinition"]
        ).BusinessTaskDefinition(task_key="health.reconcile", name="health"))
        request = svc.enqueue("health.reconcile", input_data={"x": 1})
        run = svc.create_run_for_request(request.request_id)
        assert run.status == "requested"
        assert svc.repo.db.fetchone(
            "SELECT status FROM business_job_runs WHERE run_id=?", (run.run_id,)
        )["status"] == "requested"
        completed = svc.execute_run(run.run_id)
        assert completed.status == JOB_SUCCESS

    def test_requested_run_is_not_recovered_as_stale(self, svc):
        register_task("health.reconcile", run_ok)
        request = svc.enqueue("health.reconcile", input_data={})
        run = svc.create_run_for_request(request.request_id)
        assert run.status == "requested"
        assert svc.recover_stale_runs() == 0
        assert svc.repo.db.fetchone(
            "SELECT status FROM business_job_runs WHERE run_id=?", (run.run_id,)
        )["status"] == "requested"

    def test_lock_key_contains_execution_scope(self, svc):
        key = svc.make_lock_key("simulation.run", period="2026-08", partition="2026-08", write_group="simulation")
        assert "period:2026-08" in key
        assert "partition:2026-08" in key
        assert "write:simulation" in key

    def test_heartbeat_updates_job_run(self, svc):
        register_task("health.reconcile", run_ok)
        request = svc.enqueue("health.reconcile", input_data={})
        run = svc.create_run_for_request(request.request_id)
        svc.repo.db.update("business_job_runs", {"status": JOB_RUNNING},
                           "run_id=?", (run.run_id,))
        assert svc.acquire_lock(run.lock_key, run.run_id)
        svc.heartbeat(run.lock_key, owner_run_id=run.run_id)
        row = svc.repo.db.fetchone(
            "SELECT heartbeat_at FROM business_job_runs WHERE run_id=?", (run.run_id,)
        )
        assert row["heartbeat_at"]

    def test_lock_is_held_until_handler_finishes(self, svc):
        entered = threading.Event()
        release = threading.Event()

        def blocking_handler(input_data):
            entered.set()
            assert release.wait(timeout=5)
            return {}

        register_task("blocking.run", blocking_handler)
        first = {}

        def run_first():
            first["run"] = svc.run("blocking.run", input_data={})

        thread = threading.Thread(target=run_first)
        thread.start()
        assert entered.wait(timeout=5)
        with pytest.raises(TaskStateError):
            svc.run("blocking.run", input_data={})
        release.set()
        thread.join(timeout=5)
        assert first["run"].status == JOB_SUCCESS

    def test_run_next_claims_before_executor_lookup(self, svc):
        register_task("health.reconcile", run_ok)
        request = svc.enqueue("health.reconcile", input_data={})
        run = svc.create_run_for_request(request.request_id)
        claimed = svc._claim_run()
        assert claimed.run_id == run.run_id
        assert claimed.status == JOB_RUNNING
        assert svc.repo.db.fetchone(
            "SELECT status FROM business_job_runs WHERE run_id=?", (run.run_id,)
        )["status"] == JOB_RUNNING
        assert svc._claim_run() is None
        svc.release_lock(claimed.lock_key, owner_run_id=claimed.run_id)

    def test_release_only_by_owner(self, svc):
        assert svc.acquire_lock("task:x", "owner")
        svc.release_lock("task:x", owner_run_id="other")
        assert not svc.acquire_lock("task:x", "other")
        svc.release_lock("task:x", owner_run_id="owner")
        assert svc.acquire_lock("task:x", "other")
