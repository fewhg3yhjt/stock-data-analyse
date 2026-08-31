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
            "heartbeat_at": "", "finished_at": "", "error_code": "", "error_message": "",
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

    def test_release_only_by_owner(self, svc):
        assert svc.acquire_lock("task:x", "owner")
        svc.release_lock("task:x", owner_run_id="other")
        assert not svc.acquire_lock("task:x", "other")
        svc.release_lock("task:x", owner_run_id="owner")
        assert svc.acquire_lock("task:x", "other")
