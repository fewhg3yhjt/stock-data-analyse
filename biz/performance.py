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
    net_investment: float | None = None


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
        ledger = self.pf.repo.db.fetchall(
            "SELECT * FROM cash_ledger_entries WHERE portfolio_id=? "
            "ORDER BY entry_time, rowid", (portfolio_id,))
        executions = self.pf.repo.db.fetchall(
            "SELECT * FROM executions WHERE portfolio_id=? ORDER BY trade_time, rowid", (portfolio_id,))

        # 交易日必须来自估值行情；没有行情时只能返回现金流水日期。
        if price_df is not None and not price_df.empty:
            dates = sorted({pd.to_datetime(d).strftime("%Y-%m-%d") for d in price_df["date"]})
        else:
            dates = sorted({str(row["entry_time"])[:10] for row in ledger})

        # 以交易日为轴重放，不使用当前 position_lots 的 remaining_quantity。
        cash = 0.0
        lots: dict[str, list[dict]] = {}
        ledger_index = 0
        execution_index = 0
        realized_pnl = 0.0
        fees = 0.0
        tax = 0.0
        dividend_income = 0.0
        points: list[EquityPoint] = []

        # INITIAL defines starting capital, not a dated cash movement. It must
        # remain available even when a historical transaction is backfilled.
        initial_entries = [entry for entry in ledger if entry["entry_type"] == "INITIAL"]
        cash += sum(float(entry["amount"] or 0.0) for entry in initial_entries)
        ledger = [entry for entry in ledger if entry["entry_type"] != "INITIAL"]

        for date in dates:
            external_cash_flow = 0.0
            while ledger_index < len(ledger) and str(ledger[ledger_index]["entry_time"])[:10] <= date:
                entry = ledger[ledger_index]
                amount = float(entry["amount"] or 0.0)
                cash += amount
                if entry["entry_type"] in {"ADJUSTMENT", "CORRECTION"}:
                    external_cash_flow += amount
                ledger_index += 1

            while execution_index < len(executions) and str(executions[execution_index]["trade_time"])[:10] <= date:
                execution = executions[execution_index]
                realized_delta, fee_delta, tax_delta, dividend_delta = self._replay_execution(lots, execution)
                realized_pnl += realized_delta
                fees += fee_delta
                tax += tax_delta
                dividend_income += dividend_delta
                execution_index += 1

            market_value = 0.0
            if price_df is not None and not price_df.empty:
                day_df = price_df[pd.to_datetime(price_df["date"]).dt.strftime("%Y-%m-%d") == date]
                for symbol, symbol_lots in lots.items():
                    quantity = sum(float(lot["quantity"]) for lot in symbol_lots if lot["quantity"] > 0)
                    if quantity <= 0:
                        continue
                    prices = day_df[day_df["code"].astype(str).map(self._safe_normalize) == symbol]
                    if not prices.empty and pd.notna(prices["close"].iloc[-1]):
                        market_value += quantity * float(prices["close"].iloc[-1])
            cost_basis = sum(
                float(lot["quantity"]) * float(lot["price"]) + float(lot.get("entry_fee", 0.0))
                for symbol_lots in lots.values() for lot in symbol_lots
                if lot["quantity"] > 0
            )
            unrealized_pnl = market_value - cost_basis
            points.append(EquityPoint(
                date=date, cash=cash, market_value=market_value, equity=cash + market_value,
                external_cash_flow=external_cash_flow, realized_pnl=realized_pnl,
                unrealized_pnl=unrealized_pnl, fees=fees, dividend_income=dividend_income,
            ))
        return points

    @staticmethod
    def _safe_normalize(value: str) -> str:
        from StockInvestmentTool.biz.code import normalize
        try:
            return normalize(value)
        except ValueError:
            return value

    @staticmethod
    def _replay_execution(lots: dict[str, list[dict]], execution) -> tuple[float, float, float, float]:
        """将一笔历史交易应用到内存 Lot 状态，按 FIFO 重放。

        返回本次执行产生的 realized_pnl、fee、tax、dividend_income。
        """
        symbol = PerformanceService._safe_normalize(str(execution["symbol"]))
        event_type = execution["event_type"]
        quantity = float(execution["quantity"] or 0.0)
        fee = float(execution["fee"] or 0.0)
        tax = float(execution["tax"] or 0.0)
        price = float(execution["price"] or 0.0)
        if event_type == "BUY":
            lots.setdefault(symbol, []).append({
                "quantity": quantity, "price": price, "entry_fee": fee + tax,
            })
        elif event_type == "SELL":
            remaining = quantity
            gross = quantity * price
            realized = 0.0
            for lot in lots.get(symbol, []):
                if remaining <= 0:
                    break
                consumed = min(remaining, lot["quantity"])
                lot["quantity"] -= consumed
                entry_fee = float(lot.get("entry_fee", 0.0)) * consumed / float(lot["quantity"] + consumed or 1.0)
                realized += consumed * price - consumed * float(lot["price"]) - entry_fee
                remaining -= consumed
            realized -= fee + tax
            return realized, fee, tax, 0.0
        elif event_type == "BONUS_SHARE":
            for lot in lots.get(symbol, []):
                if lot["quantity"] > 0:
                    lot["quantity"] += quantity
        elif event_type == "STOCK_SPLIT":
            for lot in lots.get(symbol, []):
                if lot["quantity"] > 0:
                    lot["quantity"] *= quantity
                    lot["price"] /= quantity
        elif event_type == "RIGHTS_ISSUE":
            for lot in lots.get(symbol, []):
                if lot["quantity"] > 0:
                    lot["quantity"] += quantity
        elif event_type == "CASH_DIVIDEND":
            return 0.0, fee, tax, quantity
        return 0.0, fee, tax, 0.0

    def compute(self, portfolio_id: str, start_date: str, end_date: str,
                price_df: pd.DataFrame | None = None,
                benchmark_return: float | None = None) -> PerformanceResult:
        curve = [point for point in self.equity_curve(portfolio_id, price_df)
                 if start_date <= point.date <= end_date]
        if not curve:
            return PerformanceResult(portfolio_id=portfolio_id, start_date=start_date,
                                     end_date=end_date, comparison_status="unavailable")

        begin_equity = curve[0].equity
        end_equity = curve[-1].equity
        # 期间外部现金流不计入投资收益；首个点的 INITIAL 现金是期初资本。
        # The first valuation point establishes beginning capital; cash flows
        # after that point are excluded from investment return.
        external_flows = [float(point.external_cash_flow or 0.0) for point in curve[1:]]
        net_external_flow = sum(external_flows)
        invested_capital = begin_equity + sum(flow for flow in external_flows if flow > 0)
        net_pnl = end_equity - begin_equity - net_external_flow
        total_return = net_pnl / invested_capital if invested_capital else None
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
            net_investment=invested_capital,
        )

    def save_result(self, result: PerformanceResult, repo=None) -> str:
        """保存 PerformanceSnapshot 与 Comparison 的基础结果。"""
        if repo is None:
            repo = self.pf.repo
        query_id = new_id("pquery")
        for point in result.equity_curve:
            repo.db.insert("performance_snapshots", {
                "snapshot_id": new_id("psnap"), "query_id": query_id,
                "as_of": point["date"], "cash": point["cash"],
                "market_value": point["market_value"], "equity": point["equity"],
                "net_investment": point.get("external_cash_flow", 0.0),
                "realized_pnl": point.get("realized_pnl"),
                "unrealized_pnl": point.get("unrealized_pnl"),
                "fees": point.get("fees", 0.0), "return_rate": result.total_return,
                "data_context_json": "{}", "created_at": now_utc(),
            })
        repo.db.insert("performance_comparisons", {
            "comparison_id": new_id("comparison"), "portfolio_id": result.portfolio_id,
            "cycle_id": "", "actual_return": result.total_return,
            "simulation_return": None, "benchmark_return": result.benchmark_return,
            "actual_excess_vs_benchmark": result.excess_return,
            "actual_gap_vs_simulation": None, "simulation_excess_vs_benchmark": None,
            "start_date": result.start_date, "end_date": result.end_date,
            "assumptions_json": "{}", "created_at": now_utc(),
        })
        return query_id

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
        # Persist a stable evidence index at creation time; detailed snapshots
        # remain owned by their source modules.
        cycle = self.repo.db.fetchone(
            "SELECT observation_id, simulation_run_id FROM position_cycles WHERE position_cycle_id=?",
            (position_cycle_id,),
        )
        if cycle:
            for source_type, source_id in (
                ("observation", cycle["observation_id"]),
                ("simulation_run", simulation_run_id or cycle["simulation_run_id"]),
            ):
                if source_id:
                    self.add_evidence(review.review_id, source_type=source_type, source_id=source_id)
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
