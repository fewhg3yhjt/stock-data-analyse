# -*- coding: utf-8 -*-
"""business.db repository：新业务实体持久化。

依据 docs/NEW_SYSTEM_STORAGE_DESIGN.md。所有业务事实只写 business.db，
不写 portfolio.db / meta.db / management.db 的业务表。
"""

from __future__ import annotations

import logging
from typing import Any

from StockInvestmentTool.biz.db import BusinessDB, dumps_json, loads_json, now_utc
from StockInvestmentTool.biz.models import (
    SimulationEvent,
    SimulationFill,
    SimulationLot,
    SimulationResult,
    SimulationRun,
    StrategyDecision,
    new_id,
)
from StockInvestmentTool.biz.regime import MarketRegime
from StockInvestmentTool.biz.research import ResearchEvidence, ResearchResult
from StockInvestmentTool.biz.screen import ScreenCandidate, ScreenDefinition, ScreenRun

logger = logging.getLogger(__name__)


class BusinessRepository:
    """business.db 仓储。所有写操作走 BusinessDB。"""

    def __init__(self, db: BusinessDB | None = None):
        self.db = db or BusinessDB()

    # ── 策略 ──────────────────────────────────────────────

    def save_strategy_version(self, strategy_id: str, version_no: int, config: dict,
                              config_hash: str, status: str = "draft") -> str:
        vid = new_id("sv")
        ts = now_utc()
        self.db.upsert("strategies", {
            "strategy_id": strategy_id, "name": config.get("name", strategy_id),
            "status": status, "current_version_id": vid, "created_at": ts, "updated_at": ts,
        }, "strategy_id")
        self.db.upsert("strategy_versions", {
            "strategy_version_id": vid, "strategy_id": strategy_id, "version_no": version_no,
            "config_json": dumps_json(config), "config_hash": config_hash, "status": status,
            "published_at": "", "enabled_at": "", "created_at": ts, "updated_at": ts,
        }, "strategy_version_id")
        return vid

    def publish_strategy_version(self, strategy_version_id: str) -> None:
        row = self.get_strategy_version(strategy_version_id)
        if not row:
            raise KeyError(f"unknown strategy version: {strategy_version_id}")
        if row["status"] not in {"validated", "published", "enabled"}:
            raise ValueError("strategy version must be validated before publish")
        self.db.update("strategy_versions", {"status": "published", "published_at": now_utc()},
                       "strategy_version_id=?", (strategy_version_id,))
        self.db.update("strategies", {"status": "published", "current_version_id": strategy_version_id,
                                      "updated_at": now_utc()},
                       "strategy_id=?", (row["strategy_id"],))

    def get_strategy_version(self, strategy_version_id: str) -> dict | None:
        row = self.db.fetchone(
            "SELECT * FROM strategy_versions WHERE strategy_version_id=?", (strategy_version_id,))
        return dict(row) if row else None

    def list_strategy_versions(self, strategy_id: str) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT * FROM strategy_versions WHERE strategy_id=? ORDER BY version_no", (strategy_id,))
        return [dict(r) for r in rows]

    # ── 决策 ──────────────────────────────────────────────

    def save_decision(self, decision: StrategyDecision) -> str:
        ts = now_utc()
        if not decision.strategy_version_id:
            raise ValueError("strategy_version_id is required for persisted decision")
        if not self.get_strategy_version(decision.strategy_version_id):
            raise ValueError(f"unknown strategy version: {decision.strategy_version_id}")
        self.db.insert("strategy_decisions", {
            "decision_id": decision.decision_id,
            "strategy_version_id": decision.strategy_version_id or "",
            "research_run_id": decision.research_run_id or "",
            "simulation_run_id": decision.simulation_run_id or "",
            "observation_id": decision.observation_id or "",
            "position_cycle_id": decision.position_cycle_id or "",
            "symbol": decision.symbol,
            "decision_time": decision.decision_time,
            "data_as_of": decision.data_as_of,
            "action": decision.action,
            "quantity": decision.quantity_ratio,
            "quantity_ratio": decision.quantity_ratio,
            "price": decision.price,
            "stop_price": decision.stop_price,
            "target_price": decision.target_price,
            "input_snapshot_json": dumps_json(decision.input_snapshot),
            "decision_trace_json": dumps_json(decision.decision_trace),
            "reason": decision.reason,
            "valid_until": decision.valid_until or "",
            "created_at": ts,
        })
        return decision.decision_id

    def get_decision(self, decision_id: str) -> dict | None:
        row = self.db.fetchone("SELECT * FROM strategy_decisions WHERE decision_id=?", (decision_id,))
        if not row:
            return None
        d = dict(row)
        d["input_snapshot"] = loads_json(d.pop("input_snapshot_json"))
        d["decision_trace"] = loads_json(d.pop("decision_trace_json"))
        return d

    # ── 市场状态 ──────────────────────────────────────────

    def save_market_regime(self, regime: MarketRegime) -> str:
        self.db.upsert("market_regimes", {
            "regime_id": regime.regime_id, "regime": regime.regime, "as_of": regime.as_of,
            "confidence": regime.confidence, "algorithm_version": regime.algorithm_version,
            "input_snapshot_json": dumps_json(regime.input_snapshot),
            "explanation": regime.explanation,
            "data_context_json": dumps_json(regime.data_context),
            "created_at": regime.created_at,
        }, "regime_id")
        return regime.regime_id

    def get_market_regime(self, as_of: str, algorithm_version: str = "market_regime.v1") -> dict | None:
        row = self.db.fetchone(
            "SELECT * FROM market_regimes WHERE as_of=? AND algorithm_version=?",
            (as_of, algorithm_version))
        if not row:
            return None
        d = dict(row)
        d["input_snapshot"] = loads_json(d.pop("input_snapshot_json"))
        d["data_context"] = loads_json(d.pop("data_context_json"))
        return d

    # ── 筛选 ──────────────────────────────────────────────

    def save_screen_version(self, definition: ScreenDefinition) -> str:
        svid = new_id("scv")
        ts = now_utc()
        config = {
            "name": definition.name, "description": definition.description,
            "asset_types": definition.asset_types, "condition_spec": definition.condition_spec,
            "sort_spec": definition.sort_spec, "display_fields": definition.display_fields,
        }
        self.db.upsert("screens", {
            "screen_id": definition.screen_id, "name": definition.name,
            "status": "draft", "current_version_id": "", "created_at": ts, "updated_at": ts,
        }, "screen_id")
        self.db.upsert("screen_versions", {
            "screen_version_id": svid, "screen_id": definition.screen_id,
            "version_no": int(definition.version or 1), "config_json": dumps_json(config),
            "config_hash": definition.config_hash(), "status": "draft",
            "published_at": "", "created_at": ts,
        }, "screen_version_id")
        return svid

    def publish_screen_version(self, screen_version_id: str) -> None:
        row = self.db.fetchone("SELECT * FROM screen_versions WHERE screen_version_id=?", (screen_version_id,))
        if not row:
            raise KeyError(f"unknown screen version: {screen_version_id}")
        if row["status"] not in {"validated", "published"}:
            raise ValueError("screen version must be validated before publish")
        self.db.update("screen_versions", {"status": "published", "published_at": now_utc()},
                       "screen_version_id=?", (screen_version_id,))
        self.db.update("screens", {"status": "published", "current_version_id": screen_version_id,
                                   "updated_at": now_utc()},
                       "screen_id=?", (row["screen_id"],))

    def save_universe_snapshot(self, symbols: list[str], universe_type: str = "selected_symbols",
                               as_of: str = "") -> str:
        usid = new_id("univ")
        import hashlib
        fingerprint = hashlib.sha256(
            dumps_json(sorted(symbols)).encode("utf-8")).hexdigest()
        self.db.insert("universe_snapshots", {
            "universe_snapshot_id": usid, "universe_type": universe_type,
            "symbols_json": dumps_json(sorted(symbols)), "symbol_count": len(symbols),
            "fingerprint": fingerprint, "as_of": as_of, "created_at": now_utc(),
        })
        return usid

    def save_screen_run(self, run: ScreenRun) -> str:
        self.db.insert("screen_runs", {
            "screen_run_id": run.run_id,
            "screen_version_id": run.screen_version_id,
            "universe_snapshot_id": run.universe_snapshot_id,
            "run_type": run.run_type, "requested_as_of": run.requested_as_of,
            "actual_data_as_of": run.actual_data_as_of,
            "data_context_json": dumps_json(run.data_context),
            "status": run.status, "matched_count": run.matched_count,
            "started_at": run.started_at, "finished_at": run.finished_at, "error": run.error,
        })
        return run.run_id

    def save_screen_candidate(self, candidate: ScreenCandidate) -> str:
        self.db.insert("screen_candidates", {
            "candidate_id": candidate.candidate_id,
            "screen_run_id": candidate.screen_run_id, "symbol": candidate.symbol,
            "name": candidate.name, "asset_type": candidate.asset_type,
            "industry": candidate.industry, "rank_no": candidate.rank_no,
            "score": candidate.score, "matched": candidate.matched,
            "condition_results_json": dumps_json(candidate.condition_results),
            "display_values_json": dumps_json(candidate.display_values),
            "data_as_of": candidate.data_as_of, "expires_at": candidate.expires_at or "",
            "created_at": now_utc(),
        })
        return candidate.candidate_id

    def list_candidates(self, screen_run_id: str) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT * FROM screen_candidates WHERE screen_run_id=? ORDER BY rank_no",
            (screen_run_id,))
        out = []
        for r in rows:
            d = dict(r)
            d["condition_results"] = loads_json(d.pop("condition_results_json"))
            d["display_values"] = loads_json(d.pop("display_values_json"))
            out.append(d)
        return out

    # ── 研究 ──────────────────────────────────────────────

    def save_research_run(self, result: ResearchResult, data_context: dict,
                          subject_type: str = "single_symbol", symbol: str = "",
                          strategy_version_id: str = "", observation_id: str = "",
                          source_screen_run_id: str = "", source_candidate_id: str = "") -> str:
        ts = now_utc()
        self.db.insert("research_runs", {
            "research_run_id": result.research_run_id, "subject_type": subject_type,
            "symbol": symbol, "observation_id": observation_id,
            "source_screen_run_id": source_screen_run_id, "source_candidate_id": source_candidate_id,
            "strategy_version_id": strategy_version_id,
            "data_context_json": dumps_json(data_context),
            "status": result.status,
            "result_json": dumps_json({
                "technical_assessment": result.technical_assessment,
                "valuation_assessment": result.valuation_assessment,
                "fundamental_assessment": result.fundamental_assessment,
                "market_assessment": result.market_assessment,
                "entry_plan": result.entry_plan, "exit_plan": result.exit_plan,
                "strategy_decision_ids": result.strategy_decision_ids,
                "warnings": result.warnings,
            }),
            "started_at": result.started_at, "finished_at": result.finished_at, "error": result.error,
        })
        for ev in (result.evidences or self.evidences_to_save(result)):
            if not ev.research_run_id:
                ev.research_run_id = result.research_run_id
            self.save_research_evidence(ev)
        return result.research_run_id

    def evidences_to_save(self, result: ResearchResult) -> list[ResearchEvidence]:
        # ResearchService 已生成 evidence 列表，此处重建（repository 与 service 解耦）
        evs: list[ResearchEvidence] = []
        for etype, assessment in (
            ("technical", result.technical_assessment),
            ("valuation", result.valuation_assessment),
            ("fundamental", result.fundamental_assessment),
            ("market", result.market_assessment),
        ):
            evs.append(ResearchEvidence(
                evidence_id=new_id("ev"), research_run_id=result.research_run_id,
                evidence_type=etype, assessment=str(assessment.get("status", "")),
                explanation=str(assessment), data_as_of="",
            ))
        return evs

    def save_research_evidence(self, ev: ResearchEvidence) -> str:
        self.db.insert("research_evidence", {
            "evidence_id": ev.evidence_id, "research_run_id": ev.research_run_id,
            "evidence_type": ev.evidence_type, "source": ev.source,
            "metric_name": ev.metric_name, "actual_value": ev.actual_value,
            "threshold_value": ev.threshold_value, "assessment": ev.assessment,
            "explanation": ev.explanation, "data_as_of": ev.data_as_of,
            "input_snapshot_json": dumps_json(ev.input_snapshot), "created_at": now_utc(),
        })
        return ev.evidence_id

    def get_research_run(self, research_run_id: str) -> dict | None:
        row = self.db.fetchone("SELECT * FROM research_runs WHERE research_run_id=?", (research_run_id,))
        if not row:
            return None
        d = dict(row)
        d["data_context"] = loads_json(d.pop("data_context_json"))
        d["result"] = loads_json(d.pop("result_json"))
        return d

    def save_research_report(self, research_run_id: str, content: str,
                             format: str = "markdown") -> str:
        report_id = new_id("report")
        self.db.insert("research_reports", {
            "report_id": report_id, "research_run_id": research_run_id,
            "format": format, "content": content, "created_at": now_utc(),
        })
        return report_id

    def get_research_report(self, research_run_id: str) -> dict | None:
        row = self.db.fetchone(
            "SELECT * FROM research_reports WHERE research_run_id=? ORDER BY rowid DESC LIMIT 1",
            (research_run_id,),
        )
        return dict(row) if row else None

    def list_research_evidence(self, research_run_id: str) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT * FROM research_evidence WHERE research_run_id=?", (research_run_id,))
        out = []
        for r in rows:
            d = dict(r)
            d["input_snapshot"] = loads_json(d.pop("input_snapshot_json"))
            out.append(d)
        return out

    # ── 模拟 ──────────────────────────────────────────────

    def save_simulation_plan(self, plan) -> str:
        self.db.insert("simulation_plans", {
            "plan_id": plan.plan_id,
            "strategy_version_id": plan.strategy_version_id,
            "name": plan.name,
            "universe_snapshot_id": plan.universe_snapshot_id or "",
            "source_screen_run_id": plan.source_screen_run_id or "",
            "observation_id": plan.observation_id or "",
            "start_date": plan.start_date,
            "end_date": plan.end_date,
            "initial_cash": plan.initial_cash,
            "position_sizing_json": dumps_json(plan.position_sizing),
            "execution_rules_json": dumps_json(plan.execution_rules),
            "cost_config_json": dumps_json(plan.cost_config),
            "benchmark": plan.benchmark,
            "data_context_json": dumps_json(plan.data_context),
            "created_at": plan.created_at,
            "updated_at": plan.updated_at,
        })
        return plan.plan_id

    def save_simulation_run(self, run: SimulationRun) -> str:
        self.db.insert("simulation_runs", {
            "run_id": run.run_id, "plan_id": run.plan_id, "status": run.status,
            "data_context_json": dumps_json(run.data_context),
            "started_at": run.started_at or "", "finished_at": run.finished_at or "",
            "error": run.error,
        })
        return run.run_id

    def update_simulation_run_status(self, run_id: str, status: str, error: str = "") -> None:
        self.db.update("simulation_runs",
                       {"status": status, "error": error,
                        "finished_at": now_utc() if status in {"success", "failed", "cancelled"} else ""},
                       "run_id=?", (run_id,))

    def save_simulation_fill(self, fill: SimulationFill) -> str:
        self.db.insert("simulation_fills", {
            "fill_id": fill.fill_id, "simulation_run_id": fill.simulation_run_id,
            "symbol": fill.symbol, "side": fill.side,
            "signal_time": fill.signal_time, "execution_time": fill.execution_time or "",
            "signal_price": fill.signal_price, "execution_price": fill.execution_price,
            "quantity": fill.quantity, "gross_amount": fill.gross_amount,
            "fee": fill.fee, "tax": fill.tax, "slippage": fill.slippage,
            "decision_id": fill.decision_id or "", "reason": fill.reason,
            "created_at": fill.created_at,
        })
        return fill.fill_id

    def save_simulation_lot(self, lot: SimulationLot) -> str:
        self.db.insert("simulation_lots", {
            "lot_id": lot.lot_id,
            "simulation_run_id": lot.simulation_run_id,
            "symbol": lot.symbol,
            "opened_at": lot.opened_at,
            "quantity": lot.quantity,
            "remaining_quantity": lot.remaining_quantity,
            "entry_price": lot.entry_price,
            "entry_fee": lot.entry_fee,
            "source_fill_id": lot.source_fill_id or "",
            "created_at": now_utc(),
        })
        return lot.lot_id

    def list_simulation_lots(self, run_id: str) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT * FROM simulation_lots WHERE simulation_run_id=? ORDER BY opened_at, lot_id",
            (run_id,),
        )
        return [dict(row) for row in rows]

    def save_simulation_result(self, result: SimulationResult) -> str:
        self.db.insert("simulation_results", {
            "result_id": new_id("res"), "run_id": result.run_id,
            "initial_cash": result.initial_cash, "final_equity": result.final_equity,
            "total_return": result.total_return, "benchmark_return": result.benchmark_return,
            "excess_return": result.excess_return, "max_drawdown": result.max_drawdown,
            "win_rate": result.win_rate, "profit_factor": result.profit_factor,
            "trade_count": result.trade_count, "average_holding_days": result.average_holding_days,
            "fees": result.fees, "slippage": result.slippage,
            "equity_curve_json": dumps_json(result.equity_curve),
            "comparison_status": result.comparison_status, "created_at": result.created_at,
        })
        return result.run_id

    def get_simulation_result(self, run_id: str) -> dict | None:
        row = self.db.fetchone("SELECT * FROM simulation_results WHERE run_id=?", (run_id,))
        if not row:
            return None
        d = dict(row)
        d["equity_curve"] = loads_json(d.pop("equity_curve_json"))
        return d

    def list_simulation_fills(self, run_id: str) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT * FROM simulation_fills WHERE simulation_run_id=? ORDER BY execution_time, fill_id",
            (run_id,))
        return [dict(r) for r in rows]

    def save_simulation_event(self, event: SimulationEvent) -> str:
        """持久化模拟事件（B8：新 biz 链路事件不再只存内存）。"""
        self.db.insert("simulation_events", {
            "event_id": event.event_id,
            "simulation_run_id": event.simulation_run_id,
            "symbol": event.symbol,
            "event_type": event.event_type,
            "payload_json": dumps_json(event.payload),
            "event_time": event.event_time,
        })
        return event.event_id

    def save_simulation_events(self, events: list[SimulationEvent]) -> int:
        """批量持久化模拟事件，逐条幂等写入。"""
        for event in events:
            self.save_simulation_event(event)
        return len(events)

    def list_simulation_events(self, run_id: str) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT * FROM simulation_events WHERE simulation_run_id=? ORDER BY event_time, event_id",
            (run_id,))
        result = []
        for r in rows:
            d = dict(r)
            d["payload"] = loads_json(d.pop("payload_json"))
            result.append(d)
        return result
