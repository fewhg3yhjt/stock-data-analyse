# -*- coding: utf-8 -*-

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.scheduler import BusinessScheduler


class FakeJob:
    def __init__(self, job_id):
        self.id = job_id


class FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, func, trigger, id, replace_existing, kwargs, **options):
        self.jobs[id] = (func, kwargs, options)

    def get_jobs(self):
        return [FakeJob(job_id) for job_id in self.jobs]


def test_business_scheduler_only_enqueues(tmp_path):
    scheduler = FakeScheduler()
    service = BusinessScheduler(
        scheduler, BusinessRepository(BusinessDB(tmp_path / "scheduler.db"))
    )
    service.register_interval("health.reconcile", minutes=10)
    request_id = service.enqueue("health.reconcile", {"source": "test"})
    row = service.service.repo.db.fetchone(
        "SELECT * FROM business_execution_requests WHERE request_id=?", (request_id,)
    )
    run = service.service.repo.db.fetchone(
        "SELECT * FROM business_job_runs WHERE request_id=?", (request_id,)
    )
    assert row is not None
    assert run["status"] == "requested"
    assert service.registered()[0]["registered"] is True


def test_web_scheduler_registers_only_real_business_maintenance_task(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from StockInvestmentTool.web.scheduler import _schedule_business_tasks

    monkeypatch.setenv("BUSINESS_DB_PATH", str(tmp_path / "business.db"))
    monkeypatch.setenv("BUSINESS_EXPIRY_RECONCILE_MINUTES", "5")
    scheduler = FakeScheduler()
    app = SimpleNamespace(extensions={})

    _schedule_business_tasks(scheduler, app)

    assert set(scheduler.jobs) == {
        "biz:observation.expiry_reconcile",
        "biz:notification.outbox_delivery",
        "biz:report.daily_generate",
    }
    state = app.extensions["business_scheduler_state"]
    assert state["enabled"] is True
    assert state["expiry_reconcile_minutes"] == 5
    assert state["outbox_delivery_minutes"] == 1


def test_web_scheduler_can_be_disabled(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from StockInvestmentTool.web.scheduler import _schedule_business_tasks

    monkeypatch.setenv("BUSINESS_DB_PATH", str(tmp_path / "business.db"))
    monkeypatch.setenv("BUSINESS_SCHEDULER_ENABLED", "0")
    scheduler = FakeScheduler()
    app = SimpleNamespace(extensions={})

    _schedule_business_tasks(scheduler, app)

    assert scheduler.jobs == {}
    assert app.extensions["business_scheduler_state"]["reason"] == "disabled_by_config"
