# -*- coding: utf-8 -*-

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.task_registry import register_business_tasks
from StockInvestmentTool.biz.tasks import BusinessTaskService


def test_worker_executes_requested_run(tmp_path):
    service = BusinessTaskService(BusinessRepository(BusinessDB(tmp_path / "worker.db")))
    register_business_tasks(service)
    request = service.enqueue("health.reconcile", input_data={})
    run = service.create_run_for_request(request.request_id)
    result = service.run_next()
    assert result.run_id == run.run_id
    assert result.status == "success"
