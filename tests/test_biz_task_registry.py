# -*- coding: utf-8 -*-

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.task_registry import BUSINESS_TASK_DEFINITIONS, register_business_tasks
from StockInvestmentTool.biz.tasks import BusinessTaskService


def test_registers_all_business_tasks(tmp_path):
    service = BusinessTaskService(BusinessRepository(BusinessDB(tmp_path / "tasks.db")))
    register_business_tasks(service)
    rows = service.repo.db.fetchall("SELECT task_key FROM business_task_definitions ORDER BY task_key")
    assert [row["task_key"] for row in rows] == sorted(key for key, _, _ in BUSINESS_TASK_DEFINITIONS)


def test_health_task_runs_through_business_runner(tmp_path):
    service = BusinessTaskService(BusinessRepository(BusinessDB(tmp_path / "tasks2.db")))
    register_business_tasks(service)
    run = service.run("health.reconcile", input_data={})
    assert run.status == "success"
    assert service.list_runs("health.reconcile")[0]["status"] == "success"
