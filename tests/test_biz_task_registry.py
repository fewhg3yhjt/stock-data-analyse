# -*- coding: utf-8 -*-

import pytest

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


def test_screen_handler_runs_through_worker_with_injected_data(tmp_path, monkeypatch):
    import pandas as pd

    from StockInvestmentTool.warehouse.datasets import DatasetResult

    data = pd.DataFrame({
        "date": pd.to_datetime(["2026-08-14"]), "code": ["sh600908"],
        "open": [10.0], "high": [12.0], "low": [9.0], "close": [11.0],
        "volume": [1000], "amount": [10000],
    })
    monkeypatch.setattr(
        "StockInvestmentTool.biz.task_registry.load_market_data",
        lambda *args, **kwargs: DatasetResult(data=data, context={"quality_status": "PASS"}),
        raising=False,
    )
    # The handler imports load_market_data locally, so patch its data module.
    monkeypatch.setattr(
        "StockInvestmentTool.biz.data_access.load_market_data",
        lambda *args, **kwargs: DatasetResult(data=data, context={"quality_status": "PASS"}),
    )
    service = BusinessTaskService(BusinessRepository(BusinessDB(tmp_path / "screen.db")))
    register_business_tasks(service)
    request = service.enqueue("screen.run", input_data={
        "name": "task screen", "start_date": "2026-08-01", "as_of": "2026-08-14",
        "condition_spec": {"type": "comparison", "left": {"field": "close"},
                           "operator": ">", "right": {"value": 10}},
    })
    service.create_run_for_request(request.request_id)
    run = service.run_next()
    assert run.status == "success"
    screen_run_id = service.repo.db.fetchone(
        "SELECT output_versions_json FROM business_job_runs WHERE run_id=?", (run.run_id,)
    )["output_versions_json"]
    assert "screen_run_id" in screen_run_id


def test_simulation_handler_persists_failed_run_and_partial_events(tmp_path, monkeypatch):
    import pandas as pd

    from StockInvestmentTool.biz.models import SimulationEvent

    data = pd.DataFrame({
        "date": pd.to_datetime(["2026-08-14"]), "code": ["sh600908"],
        "open": [10.0], "high": [12.0], "low": [9.0], "close": [11.0],
        "volume": [1000], "amount": [10000],
    })
    monkeypatch.setattr(
        "StockInvestmentTool.biz.data_access.load_market_data",
        lambda *args, **kwargs: __import__(
            "StockInvestmentTool.warehouse.datasets", fromlist=["DatasetResult"]
        ).DatasetResult(data=data, context={"quality_status": "PASS"}),
    )

    class FailingExecutor:
        def __init__(self, plan, frame, strategy=None, run_id=None):
            self.run_id = run_id
            self.events = [SimulationEvent(
                event_id="partial-event", simulation_run_id=run_id,
                event_type="SIGNAL_GENERATED", symbol="sh600908",
            )]

        def run(self):
            raise RuntimeError("simulated failure")

    monkeypatch.setattr("StockInvestmentTool.biz.simulation.SimulationExecutor", FailingExecutor)
    monkeypatch.setattr("StockInvestmentTool.biz.strategy.compile_strategy", lambda *args, **kwargs: object())

    from StockInvestmentTool.biz.task_registry import _simulation_handler
    from StockInvestmentTool.biz.tasks import BusinessTaskService

    service = BusinessTaskService(BusinessRepository(BusinessDB(tmp_path / "simulation.db")))
    service.repo.get_strategy_version = lambda version_id: {
        "strategy_version_id": version_id, "config_json": "{}",
    }
    handler = _simulation_handler(service)
    with pytest.raises(RuntimeError, match="simulated failure"):
        handler({
            "strategy_version_id": "sv1", "symbol": "sh600908",
            "start_date": "2026-08-01", "end_date": "2026-08-14",
        })

    run = service.repo.db.fetchone(
        "SELECT run_id,status,error FROM simulation_runs ORDER BY rowid DESC LIMIT 1"
    )
    assert run["status"] == "failed"
    assert run["error"] == "simulated failure"
    event = service.repo.db.fetchone(
        "SELECT event_type FROM simulation_events WHERE simulation_run_id=?",
        (run["run_id"],),
    )
    assert event["event_type"] == "SIGNAL_GENERATED"
