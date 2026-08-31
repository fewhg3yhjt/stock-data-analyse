# -*- coding: utf-8 -*-
"""跨模块业务应用服务。

这里负责跨实体的业务编排，不承载指标计算或数据采集逻辑。所有持久化
仍然进入 business.db，数据输入由调用方提前通过 DatasetAccess 固化。
"""

from __future__ import annotations

from dataclasses import dataclass

from StockInvestmentTool.biz.db import dumps_json, now_utc
from StockInvestmentTool.biz.models import SimulationPlan, new_id
from StockInvestmentTool.biz.observation import (
    OBS_READY_FOR_ENTRY,
    Observation,
    ObservationService,
)
from StockInvestmentTool.biz.portfolio import PortfolioService
from StockInvestmentTool.biz.repo import BusinessRepository


class WorkflowError(ValueError):
    """跨模块业务前置条件不满足。"""


@dataclass
class EntryContext:
    observation_id: str
    symbol: str
    portfolio_id: str
    strategy_version_id: str | None
    entry_plan: dict
    data_context: dict


class BusinessWorkflowService:
    """承接候选、观察、模拟和真实建仓的跨模块动作。"""

    def __init__(self, repo: BusinessRepository | None = None):
        self.repo = repo or BusinessRepository()
        self.observations = ObservationService(self.repo)
        self.portfolio = PortfolioService(self.repo)

    def observe_candidate(self, candidate_id: str) -> Observation:
        """从已持久化 ScreenCandidate 创建 Observation，禁止裸 symbol 伪造来源。"""
        row = self.repo.db.fetchone(
            """SELECT c.*, r.data_context_json, r.screen_version_id
               FROM screen_candidates c JOIN screen_runs r ON r.screen_run_id=c.screen_run_id
               WHERE c.candidate_id=?""",
            (candidate_id,),
        )
        if not row:
            raise WorkflowError("CANDIDATE_NOT_FOUND")
        from datetime import datetime, timezone
        if row["expires_at"] and ObservationService._is_expired(
            str(row["expires_at"]),
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ):
            raise WorkflowError("CANDIDATE_EXPIRED")
        return self.observations.create_observation(
            row["symbol"], name=row["name"], source_type="screen",
            screen_run_id=row["screen_run_id"], screen_candidate_id=candidate_id,
            data_as_of=row["data_as_of"],
            reason_snapshot={
                "condition_results": _loads(row["condition_results_json"]),
                "data_context": _loads(row["data_context_json"]),
            },
        )

    def create_simulation_plan(self, observation_id: str, *, strategy_version_id: str,
                               start_date: str, end_date: str, initial_cash: float,
                               benchmark: str = "sh000300", cost_config: dict | None = None) -> SimulationPlan:
        obs = self.observations.get_observation(observation_id)
        if not obs:
            raise WorkflowError("OBSERVATION_NOT_FOUND")
        if obs.status not in {"discovered", "observing", "ready_for_entry"}:
            raise WorkflowError("OBSERVATION_INVALID_TRANSITION")
        plan = SimulationPlan(
            plan_id=new_id("plan"), strategy_version_id=strategy_version_id,
            observation_id=observation_id, start_date=start_date, end_date=end_date,
            initial_cash=initial_cash, benchmark=benchmark,
            cost_config=cost_config or {},
        )
        self.repo.save_simulation_plan(plan)
        obs.current_strategy_version_id = strategy_version_id
        obs.latest_simulation_run_id = plan.plan_id
        self.observations.update_observation(obs)
        return plan

    def build_entry_context(self, observation_id: str, portfolio_id: str) -> EntryContext:
        obs = self.observations.get_observation(observation_id)
        if not obs:
            raise WorkflowError("OBSERVATION_NOT_FOUND")
        if obs.status != OBS_READY_FOR_ENTRY:
            raise WorkflowError("ENTRY_CONFIRMATION_REQUIRED")
        return EntryContext(
            observation_id=observation_id, symbol=obs.symbol, portfolio_id=portfolio_id,
            strategy_version_id=obs.current_strategy_version_id,
            entry_plan={"target_amount": obs.target_amount}, data_context={},
        )

    def record_entry(self, context: EntryContext, *, quantity: float, price: float,
                     fee: float = 0.0, tax: float = 0.0,
                     idempotency_key: str) -> tuple[object, object]:
        """EntryContext → PositionCycle + BUY Execution + Observation promoted。

        四类事实都在 business.db 的同一事务中提交。
        """
        if quantity <= 0 or price <= 0:
            raise WorkflowError("INVALID_ENTRY")
        with self.repo.db.transaction() as conn:
            duplicate = conn.execute(
                "SELECT * FROM executions WHERE portfolio_id=? AND idempotency_key=?",
                (context.portfolio_id, idempotency_key),
            ).fetchone()
            if duplicate:
                return self.portfolio._row_to_execution(duplicate), self.observations.get_observation(context.observation_id)
            obs_row = conn.execute(
                "SELECT * FROM observations WHERE observation_id=?", (context.observation_id,)
            ).fetchone()
            if not obs_row or obs_row["status"] != OBS_READY_FOR_ENTRY:
                raise WorkflowError("ENTRY_CONFIRMATION_REQUIRED")
            cycle_id = new_id("pc")
            ts = now_utc()
            conn.execute(
                """INSERT INTO position_cycles
                   (position_cycle_id,portfolio_id,symbol,status,phase,strategy_version_id,
                    observation_id,entry_plan_snapshot_json,scheme_snapshot_json,opened_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cycle_id, context.portfolio_id, context.symbol, "open", "accumulating",
                 context.strategy_version_id or "", context.observation_id,
                 dumps_json(context.entry_plan), "{}", ts, ts, ts),
            )
            execution_id = new_id("exe")
            gross = quantity * price
            total = gross + fee + tax
            cash = self.portfolio._cash_balance_conn(conn, context.portfolio_id)
            if cash < total:
                raise WorkflowError("INSUFFICIENT_CASH")
            conn.execute(
                """INSERT INTO executions
                   (execution_id,portfolio_id,position_cycle_id,symbol,event_type,trade_time,
                    quantity,price,gross_amount,fee,tax,net_amount,reason,advice_id,decision_id,
                    simulation_run_id,external_ref,idempotency_key,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (execution_id, context.portfolio_id, cycle_id, context.symbol, "BUY", ts,
                 quantity, price, gross, fee, tax, gross - fee - tax, "建仓",
                 "", "", "", "", idempotency_key, ts),
            )
            conn.execute(
                """INSERT INTO position_lots
                   (lot_id,position_cycle_id,symbol,opened_at,quantity,remaining_quantity,
                    entry_price,entry_fee,source_execution_id,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (new_id("lot"), cycle_id, context.symbol, ts, quantity, quantity,
                 price, fee + tax, execution_id, ts),
            )
            conn.execute(
                """INSERT INTO cash_ledger_entries
                   (cash_entry_id,portfolio_id,entry_type,amount,balance_after,execution_id,entry_time,reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (new_id("cash"), context.portfolio_id, "BUY", -total, cash - total,
                 execution_id, ts, "建仓", ts),
            )
            conn.execute(
                "UPDATE position_cycles SET phase='holding', updated_at=? WHERE position_cycle_id=?",
                (ts, cycle_id),
            )
            conn.execute(
                "UPDATE observations SET status='promoted', promoted_position_cycle_id=?, updated_at=? WHERE observation_id=?",
                (cycle_id, ts, context.observation_id),
            )
            conn.execute(
                """INSERT INTO observation_events
                   (event_id,observation_id,event_type,event_time,from_status,to_status,source_id,reason,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (new_id("oe"), context.observation_id, "PROMOTED_TO_POSITION", ts,
                 OBS_READY_FOR_ENTRY, "promoted", cycle_id, "真实建仓已成交", "{}"),
            )
            execution = self.portfolio._row_to_execution(conn.execute(
                "SELECT * FROM executions WHERE execution_id=?", (execution_id,)
            ).fetchone())
        return execution, self.observations.get_observation(context.observation_id)


def _loads(value):
    import json
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
