"""Simulation plan/run service with explicit state transitions."""

from __future__ import annotations

from uuid import uuid4

from StockInvestmentTool.domain.features import now_text
from StockInvestmentTool.domain.simulation import (
    ALLOWED_SIM_TRANSITIONS,
    SIM_CANCELLED,
    SIM_FAILED,
    SIM_PENDING,
    SIM_RUNNING,
    SIM_SUCCESS,
    SimulationEvent,
    SimulationPlan,
    SimulationRun,
    SimulationTrade,
)
from StockInvestmentTool.repositories.backend_domain import BackendDomainRepository


class SimulationService:
    def __init__(self, repository: BackendDomainRepository | None = None):
        self.repository = repository or BackendDomainRepository()

    def create_plan(
        self,
        *,
        name: str,
        stock_set_id: str,
        start_date: str,
        end_date: str,
        initial_capital: float,
        buy_rule_id: str = "",
        buy_rule_snapshot: dict | None = None,
        sell_rule_id: str = "",
        sell_rule_snapshot: dict | None = None,
        position_rule_json: dict | None = None,
        execution_rule_json: dict | None = None,
        risk_rule_json: dict | None = None,
        broker_fee_profile_id: str = "",
        plan_id: str | None = None,
    ) -> SimulationPlan:
        if not self.repository.get_stock_set(stock_set_id):
            raise KeyError(f"unknown stock set: {stock_set_id}")
        plan = SimulationPlan(
            plan_id=plan_id or f"sp_{uuid4().hex}",
            name=name,
            stock_set_id=stock_set_id,
            buy_rule_id=buy_rule_id,
            buy_rule_snapshot=buy_rule_snapshot or {},
            sell_rule_id=sell_rule_id,
            sell_rule_snapshot=sell_rule_snapshot or {},
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            position_rule_json=position_rule_json or {},
            execution_rule_json=execution_rule_json or {},
            risk_rule_json=risk_rule_json or {},
            broker_fee_profile_id=broker_fee_profile_id,
        )
        return self.repository.save_simulation_plan(plan)

    def enqueue_run(
        self,
        plan_id: str,
        *,
        data_versions_json: dict | None = None,
        feature_versions_json: dict | None = None,
        run_id: str | None = None,
    ) -> SimulationRun:
        if not self.repository.get_simulation_plan(plan_id):
            raise KeyError(f"unknown simulation plan: {plan_id}")
        run = SimulationRun(
            run_id=run_id or f"sr_{uuid4().hex}",
            plan_id=plan_id,
            status=SIM_PENDING,
            data_versions_json=data_versions_json or {},
            feature_versions_json=feature_versions_json or {},
        )
        return self.repository.create_simulation_run(run)

    def start_run(self, run_id: str) -> SimulationRun:
        run = self._transition(run_id, SIM_RUNNING)
        run.started_at = run.started_at or now_text()
        return self.repository.update_simulation_run(run)

    def finish_success(self, run_id: str, *, result_summary_json: dict | None = None) -> SimulationRun:
        run = self._transition(run_id, SIM_SUCCESS)
        run.finished_at = now_text()
        run.progress = 100.0
        run.result_summary_json = result_summary_json or {}
        return self.repository.update_simulation_run(run)

    def fail_run(self, run_id: str, error_summary: str) -> SimulationRun:
        run = self._transition(run_id, SIM_FAILED)
        run.finished_at = now_text()
        run.error_summary = error_summary
        return self.repository.update_simulation_run(run)

    def cancel_run(self, run_id: str) -> SimulationRun:
        run = self._transition(run_id, SIM_CANCELLED)
        run.finished_at = now_text()
        return self.repository.update_simulation_run(run)

    def update_progress(self, run_id: str, progress: float) -> SimulationRun:
        run = self._get_run(run_id)
        if run.status != SIM_RUNNING:
            raise ValueError("simulation progress can only be updated while RUNNING")
        run.progress = progress
        return self.repository.update_simulation_run(run)

    def record_trade(self, run_id: str, trade: SimulationTrade) -> SimulationTrade:
        run = self._get_run(run_id)
        if run.status not in {SIM_RUNNING, SIM_SUCCESS}:
            raise ValueError("simulation trades can only be recorded for running or successful runs")
        if trade.run_id != run_id:
            raise ValueError("trade.run_id does not match run_id")
        return self.repository.add_simulation_trade(trade)

    def record_event(self, run_id: str, event: SimulationEvent) -> SimulationEvent:
        run = self._get_run(run_id)
        if run.status not in {SIM_PENDING, SIM_RUNNING, SIM_SUCCESS, SIM_FAILED, SIM_CANCELLED}:
            raise ValueError(f"invalid simulation run status: {run.status}")
        if event.run_id != run_id:
            raise ValueError("event.run_id does not match run_id")
        return self.repository.add_simulation_event(event)

    def make_trade(self, run_id: str, instrument: str, **kwargs) -> SimulationTrade:
        return SimulationTrade(trade_id=f"st_{uuid4().hex}", run_id=run_id, instrument=instrument, **kwargs)

    def make_event(self, run_id: str, event_type: str, *, instrument: str = "", payload_json: dict | None = None) -> SimulationEvent:
        return SimulationEvent(
            event_id=f"se_{uuid4().hex}",
            run_id=run_id,
            event_time=now_text(),
            instrument=instrument,
            event_type=event_type,
            payload_json=payload_json or {},
        )

    def _transition(self, run_id: str, next_status: str) -> SimulationRun:
        run = self._get_run(run_id)
        allowed = ALLOWED_SIM_TRANSITIONS.get(run.status, set())
        if next_status not in allowed:
            raise ValueError(f"invalid simulation transition: {run.status} -> {next_status}")
        run.status = next_status
        return run

    def _get_run(self, run_id: str) -> SimulationRun:
        run = self.repository.get_simulation_run(run_id)
        if not run:
            raise KeyError(f"unknown simulation run: {run_id}")
        return run
