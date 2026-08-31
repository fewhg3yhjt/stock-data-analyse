# -*- coding: utf-8 -*-
"""ResearchService：个股/观察对象的结构化研究。

依据 docs/RESEARCH_AND_ANALYSIS_DESIGN.md。
- 输入来自 DatasetResult.data/context
- 技术 + 市场评估：可用；估值评估：降级（unavailable/partial）；基本面评估：数据契约完成前记录 deferred
- LLM 只属于报告阶段，不得覆盖结构化决策
- 生成 StrategyDecision 与 ResearchEvidence
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from StockInvestmentTool.biz.code import normalize
from StockInvestmentTool.biz.models import (
    DataContext,
    StrategyContext,
    new_id,
    now_utc,
)
from StockInvestmentTool.biz.strategy import CompiledStrategy

logger = logging.getLogger(__name__)


@dataclass
class ResearchEvidence:
    evidence_id: str
    research_run_id: str
    evidence_type: str
    source: str = ""
    metric_name: str = ""
    actual_value: float | None = None
    threshold_value: float | None = None
    assessment: str = ""
    explanation: str = ""
    data_as_of: str = ""
    input_snapshot: dict = field(default_factory=dict)


@dataclass
class ResearchResult:
    research_run_id: str
    status: str = "requested"
    technical_assessment: dict = field(default_factory=dict)
    valuation_assessment: dict = field(default_factory=dict)
    fundamental_assessment: dict = field(default_factory=dict)
    market_assessment: dict = field(default_factory=dict)
    entry_plan: dict = field(default_factory=dict)
    exit_plan: dict = field(default_factory=dict)
    strategy_decision_ids: list = field(default_factory=list)
    decisions: list = field(default_factory=list)   # StrategyDecision 对象引用
    evidences: list = field(default_factory=list)   # ResearchEvidence 对象引用
    evidence_ids: list = field(default_factory=list)
    report_id: str | None = None
    warnings: list = field(default_factory=list)
    error: str = ""
    started_at: str = field(default_factory=now_utc)
    finished_at: str = ""


class ResearchService:
    """研究服务：基于统一 DatasetResult 与策略执行研究流程。"""

    def __init__(
        self,
        df: pd.DataFrame,
        context: dict,
        strategy: CompiledStrategy | None = None,
        market_regime: dict | None = None,
    ):
        self.df = df.reset_index(drop=True)
        self.context = context
        self.strategy = strategy
        self.market_regime = market_regime
        self.evidences: list[ResearchEvidence] = []

    # ── 执行 ──────────────────────────────────────────────

    def run(self) -> ResearchResult:
        result = ResearchResult(research_run_id=new_id("rr"), status="running")
        symbol = self._symbol()

        # 1. 技术评估（可用）
        result.technical_assessment = self._technical_assessment()
        self._add_evidence("technical", result.technical_assessment, symbol)

        # 2. 市场评估（可用）
        result.market_assessment = self.market_regime or {"status": "unavailable", "reason": "无 MarketRegime"}
        self._add_evidence("market", result.market_assessment, symbol)

        # 3. 估值评估（降级）
        result.valuation_assessment = self._valuation_assessment()
        self._add_evidence("valuation", result.valuation_assessment, symbol)

        # 4. 基本面评估（延期，数据契约完成前记录 deferred）
        result.fundamental_assessment = {
            "status": "deferred",
            "reason": "fundamentals 数据契约未完成，第一版延期",
        }
        result.warnings.append("基本面研究延期：fundamentals 数据契约未完成")

        # 5. 策略决策（若有策略）
        if self.strategy is not None:
            decision = self._evaluate_strategy(symbol)
            result.strategy_decision_ids = [decision.decision_id] if decision else []
            result.decisions = [decision] if decision else []
            result.entry_plan = self._entry_plan(decision)
            result.exit_plan = self._exit_plan(decision)
            self._add_evidence("strategy_rule", {"action": decision.action, "reason": decision.reason}, symbol)
        else:
            result.warnings.append("未提供策略，未生成 StrategyDecision")

        result.status = "success"
        result.finished_at = now_utc()
        for evidence in self.evidences:
            evidence.research_run_id = result.research_run_id
        result.evidence_ids = [e.evidence_id for e in self.evidences]
        result.evidences = list(self.evidences)
        return result

    # ── 内部 ──────────────────────────────────────────────

    def _symbol(self) -> str:
        if self.df.empty or "code" not in self.df.columns:
            return ""
        return normalize(str(self.df["code"].iloc[-1]))

    def _as_of(self) -> str:
        if not self.df.empty and "date" in self.df.columns:
            return str(pd.to_datetime(self.df["date"].iloc[-1]).strftime("%Y-%m-%d"))
        return ""

    def _technical_assessment(self) -> dict:
        if self.df.empty:
            return {"status": "unavailable", "reason": "无行情数据"}
        close = self.df["close"].astype(float)
        last = float(close.iloc[-1])
        ma20 = float(self.df["ma20"].iloc[-1]) if "ma20" in self.df.columns and pd.notna(self.df["ma20"].iloc[-1]) else None
        ma60 = float(self.df["ma60"].iloc[-1]) if "ma60" in self.df.columns and pd.notna(self.df["ma60"].iloc[-1]) else None
        assessment = "unknown"
        if ma20 is not None:
            assessment = "above_ma20" if last > ma20 else "below_ma20"
        return {
            "status": "ok",
            "price": last,
            "ma20": ma20,
            "ma60": ma60,
            "assessment": assessment,
            "data_as_of": self._as_of(),
        }

    def _valuation_assessment(self) -> dict:
        # 估值依赖 valuation_daily，当前受限：仅当列存在时降级评估
        pe = self.df["pe_ttm"].iloc[-1] if "pe_ttm" in self.df.columns and pd.notna(self.df["pe_ttm"].iloc[-1]) else None
        pb = self.df["pb_mrq"].iloc[-1] if "pb_mrq" in self.df.columns and pd.notna(self.df["pb_mrq"].iloc[-1]) else None
        if pe is None and pb is None:
            return {"status": "unavailable", "reason": "估值数据受限（valuation_daily 未完成）"}
        return {"status": "degraded", "pe_ttm": float(pe), "pb_mrq": float(pb),
                "note": "估值数据降级：字段/覆盖受限"}

    def _evaluate_strategy(self, symbol: str):
        from StockInvestmentTool.biz.models import StrategyContext

        if self.df.empty:
            return None
        indicator_ctx = None
        try:
            from StockInvestmentTool.indicators.context import IndicatorContext
            from StockInvestmentTool.indicators.engine import IndicatorRegistry
            indicator_ctx = IndicatorContext(df=self.df, registry=IndicatorRegistry())
        except Exception as e:  # noqa: BLE001
            logger.debug("indicator context build failed: %s", e)
        ctx = StrategyContext(
            symbol=symbol,
            evaluation_time=now_utc(),
            data_as_of=self._as_of(),
            market_data=self.df,
            indicator_context=indicator_ctx,
            market_regime=self.market_regime,
            position_state="none",
            position_quantity=0.0,
            cash_available=0.0,
            data_context=DataContext.from_dict({
                "dataset_refs": self.context.get("partition_versions", {}),
                "quality_status": self.context.get("quality_status", "unknown"),
                "returned_end": self._as_of(),
            }) if isinstance(self.context, dict) else DataContext(),
        )
        return self.strategy.evaluate(ctx)

    def _entry_plan(self, decision) -> dict:
        if decision is None:
            return {}
        return {
            "action": decision.action,
            "quantity_ratio": decision.quantity_ratio,
            "price": decision.price,
            "reason": decision.reason,
        }

    def _exit_plan(self, decision) -> dict:
        if decision is None:
            return {}
        return {
            "action": decision.action,
            "stop_price": decision.stop_price,
            "target_price": decision.target_price,
            "reason": decision.reason,
        }

    def _add_evidence(self, etype: str, assessment: dict, symbol: str) -> None:
        self.evidences.append(ResearchEvidence(
            evidence_id=new_id("ev"),
            research_run_id="",
            evidence_type=etype,
            source="research_service",
            metric_name=etype,
            actual_value=assessment.get("price") or assessment.get("pe_ttm"),
            assessment=str(assessment.get("status", "")),
            explanation=str(assessment),
            data_as_of=self._as_of(),
            input_snapshot={"symbol": symbol},
        ))
