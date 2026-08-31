# -*- coding: utf-8 -*-
"""Performance：实际/模拟/基准三线收益 + 逐日权益曲线 + 复盘。

依据 docs/PERFORMANCE_AND_REVIEW_DESIGN.md。
- 逐日权益曲线：由 Execution + 收盘价重算
- 基准 index_daily 未发布前 comparison_status=unavailable
- 三条收益线使用相同估值时点
- Review/Evidence 关联交易周期
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from StockInvestmentTool.biz.models import new_id, now_utc
from StockInvestmentTool.biz.portfolio import PortfolioService

logger = logging.getLogger(__name__)


@dataclass
class EquityPoint:
    date: str
    cash: float
    market_value: float
    equity: float
    external_cash_flow: float = 0.0
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    fees: float = 0.0
    dividend_income: float = 0.0


@dataclass
class PerformanceResult:
    portfolio_id: str
    start_date: str
    end_date: str
    equity_curve: list = field(default_factory=list)
    total_return: float | None = None
    annualized_return: float | None = None
    max_drawdown: float | None = None
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None
    comparison_status: str = "unavailable"
    data_context: dict = field(default_factory=dict)


@dataclass
class PositionCycleReview:
    review_id: str
    position_cycle_id: str
    discovery_reason: str = ""
    research_summary: str = ""
    simulation_run_id: str | None = None
    planned_entry: dict = field(default_factory=dict)
    actual_entry: dict = field(default_factory=dict)
    planned_exit: dict = field(default_factory=dict)
    actual_exit: dict = field(default_factory=dict)
    advice_summary: str = ""
    execution_deviation: dict = field(default_factory=dict)
    result_summary: str = ""
    lessons: str = ""
    status: str = "pending"
    created_at: str = field(default_factory=now_utc)
    updated_at: str = field(default_factory=now_utc)


class PerformanceService:
    """收益分析：组合权益曲线重算 + 三线对比。"""

    def __init__(self, portfolio_service: PortfolioService | None = None):
        self.pf = portfolio_service or PortfolioService()

    # ── 逐日权益曲线重算 ──────────────────────────────────

    def equity_curve(self, portfolio_id: str, price_df: pd.DataFrame | None = None) -> list[EquityPoint]:
        """按交易日重算权益曲线。

        规则：对每个交易日 T，读取 T 日前已生效的 Execution，
        计算 Cash Balance，聚合剩余 Lot，用 T 收盘价估值。
        简化实现：基于 cash_ledger 的 balance_after 时间序列 + 每日持仓市值。
        """
        # 现金流随时间变化
        ledger = self.pf.repo.db.fetchall(
            "SELECT * FROM cash_ledger_entries WHERE portfolio_id=? ORDER BY rowid", (portfolio_id,))
        cash_at: dict[str, float] = {}
        cur_cash = 0.0
        for row in ledger:
            cur_cash = float(row["balance_after"])
            cash_at[row["entry_time"][:10]] = cur_cash

        # 持仓市值按日
        # 简化：使用当前持仓估值（P1 阶段），逐日部分依赖历史 lot 归因
        cycles = self.pf.list_cycles(portfolio_id)
        open_cycles = [c for c in cycles if c.status == "open"]

        # 构造交易日序列
        dates = []
        if price_df is not None and not price_df.empty:
            dates = [str(pd.to_datetime(d).strftime("%Y-%m-%d"))
                     for d in sorted(set(price_df["date"]))]
        else:
            dates = sorted(cash_at.keys())

        points: list[EquityPoint] = []
        for d in dates:
            cash = cash_at.get(d, cash_at.get(max(cash_at, default="")) if cash_at else 0.0)
            mv = 0.0
            if price_df is not None and not price_df.empty:
                day_df = price_df[price_df["date"].astype(str).str[:10] == d]
                for c in open_cycles:
                    summary = self.pf.position_summary(c.position_cycle_id)
                    qty = summary["quantity"]
                    if qty > 0:
                        sub = day_df[day_df["code"] == c.symbol]
                        if not sub.empty:
                            mv += qty * float(sub["close"].iloc[-1])
            points.append(EquityPoint(date=d, cash=cash, market_value=mv, equity=cash + mv))
        return points

    def compute(self, portfolio_id: str, start_date: str, end_date: str,
                price_df: pd.DataFrame | None = None,
                benchmark_return: float | None = None) -> PerformanceResult:
        curve = self.equity_curve(portfolio_id, price_df)
        if not curve:
            return PerformanceResult(portfolio_id=portfolio_id, start_date=start_date,
                                     end_date=end_date, comparison_status="unavailable")

        begin_equity = curve[0].equity
        end_equity = curve[-1].equity
        # 简化：不计外部现金流差异（P1 阶段外部现金流来自 ledger）
        total_return = (end_equity - begin_equity) / begin_equity if begin_equity else None
        max_dd = self._max_drawdown([p.equity for p in curve])
        n_days = len(curve)
        annualized = None
        if total_return is not None and n_days > 0:
            annualized = (1 + total_return) ** (252 / n_days) - 1 if total_return > -1 else None

        return PerformanceResult(
            portfolio_id=portfolio_id, start_date=start_date, end_date=end_date,
            equity_curve=[p.__dict__ for p in curve],
            total_return=total_return,
            annualized_return=annualized,
            max_drawdown=max_dd,
            benchmark_return=benchmark_return,
            excess_return=(total_return - benchmark_return) if total_return is not None and benchmark_return is not None else None,
            comparison_status="ok" if benchmark_return is not None else "unavailable",
        )

    @staticmethod
    def _max_drawdown(equities: list[float]) -> float:
        if not equities:
            return 0.0
        s = pd.Series(equities)
        peak = s.cummax()
        dd = (peak - s) / peak
        return float(dd.max()) if len(dd) else 0.0


class ReviewService:
    """复盘服务：PositionCycleReview + ReviewEvidence。"""

    def __init__(self, repo=None):
        from StockInvestmentTool.biz.repo import BusinessRepository
        self.repo = repo or BusinessRepository()

    def create_review(self, position_cycle_id: str, *, discovery_reason: str = "",
                      research_summary: str = "", simulation_run_id: str | None = None) -> PositionCycleReview:
        review = PositionCycleReview(
            review_id=new_id("rev"), position_cycle_id=position_cycle_id,
            discovery_reason=discovery_reason, research_summary=research_summary,
            simulation_run_id=simulation_run_id,
        )
        from StockInvestmentTool.biz.db import dumps_json
        self.repo.db.insert("position_cycle_reviews", {
            "review_id": review.review_id, "position_cycle_id": review.position_cycle_id,
            "discovery_reason": review.discovery_reason, "research_summary": review.research_summary,
            "simulation_run_id": review.simulation_run_id or "",
            "planned_entry_json": dumps_json(review.planned_entry),
            "actual_entry_json": dumps_json(review.actual_entry),
            "planned_exit_json": dumps_json(review.planned_exit),
            "actual_exit_json": dumps_json(review.actual_exit),
            "advice_summary": review.advice_summary,
            "execution_deviation_json": dumps_json(review.execution_deviation),
            "result_summary": review.result_summary, "lessons": review.lessons,
            "status": review.status, "created_at": review.created_at, "updated_at": review.updated_at,
        })
        return review

    def update_review(self, review: PositionCycleReview) -> None:
        from StockInvestmentTool.biz.db import dumps_json
        self.repo.db.update("position_cycle_reviews", {
            "discovery_reason": review.discovery_reason,
            "research_summary": review.research_summary,
            "simulation_run_id": review.simulation_run_id or "",
            "planned_entry_json": dumps_json(review.planned_entry),
            "actual_entry_json": dumps_json(review.actual_entry),
            "planned_exit_json": dumps_json(review.planned_exit),
            "actual_exit_json": dumps_json(review.actual_exit),
            "advice_summary": review.advice_summary,
            "execution_deviation_json": dumps_json(review.execution_deviation),
            "result_summary": review.result_summary, "lessons": review.lessons,
            "status": review.status, "updated_at": now_utc(),
        }, "review_id=?", (review.review_id,))

    def add_evidence(self, review_id: str, *, source_type: str, source_id: str,
                     summary: str = "", snapshot: dict | None = None,
                     evidence_type: str = "") -> str:
        from StockInvestmentTool.biz.db import dumps_json
        eid = new_id("rve")
        self.repo.db.insert("review_evidence", {
            "evidence_id": eid, "review_id": review_id,
            "evidence_type": evidence_type, "source_type": source_type,
            "source_id": source_id, "summary": summary,
            "snapshot_json": dumps_json(snapshot or {}), "created_at": now_utc(),
        })
        return eid

    def get_review(self, review_id: str) -> dict | None:
        row = self.repo.db.fetchone(
            "SELECT * FROM position_cycle_reviews WHERE review_id=?", (review_id,))
        if not row:
            return None
        d = dict(row)
        from StockInvestmentTool.biz.db import loads_json
        for k in ("planned_entry_json", "actual_entry_json", "planned_exit_json",
                  "actual_exit_json", "execution_deviation_json"):
            d[k[:-5]] = loads_json(d.pop(k))
        return d

    def list_evidence(self, review_id: str) -> list[dict]:
        rows = self.repo.db.fetchall(
            "SELECT * FROM review_evidence WHERE review_id=?", (review_id,))
        return [dict(r) for r in rows]