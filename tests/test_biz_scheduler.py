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
