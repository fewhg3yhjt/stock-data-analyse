# -*- coding: utf-8 -*-
"""SimulationExecutor：统一回测/模拟内核（单边 SimulationFill）。

依据 docs/STRATEGY_CORE_AND_SIMULATION_DESIGN.md §6/§7。
- 收盘计算信号 → 下一交易日开盘成交（第一版默认）
- 单边成交：SimulationFill（BUY/SELL 各一条记录）
- 手续费、滑点进入现金与收益
- 每日权益曲线
- 基准：index_daily 未发布前 comparison_status=unavailable
- 数据缺口记录 DATA_GAP
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from StockInvestmentTool.biz.models import (
    SimulationEvent,
    SimulationFill,
    SimulationPlan,
    SimulationResult,
    SimulationRun,
    new_id,
    now_utc,
)

logger = logging.getLogger(__name__)


@dataclass
class SimulationAccount:
    """虚拟账户状态。"""

    cash: float
    positions: dict = field(default_factory=dict)   # symbol -> {"qty": float, "avg_cost": float}
    equity: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0


class SimulationExecutor:
    """单标的多日模拟执行器。"""

    def __init__(
        self,
        plan: SimulationPlan,
        df: pd.DataFrame,            # 已按 symbol 过滤的行情 DataFrame（含 date/ohlcv）
        registry: Any = None,
        strategy=None,               # CompiledStrategy
    ):
        self.plan = plan
        self.df = df.reset_index(drop=True)
        self.registry = registry
        self.strategy = strategy
        self.fills: list[SimulationFill] = []
        self.events: list[SimulationEvent] = []
        self.account = SimulationAccount(cash=plan.initial_cash)
        self.equity_curve: list[dict] = []

    # ── 执行 ──────────────────────────────────────────────

    def run(self) -> SimulationResult:
        if self.strategy is None:
            raise ValueError("SimulationExecutor 需要 CompiledStrategy")
        if self.df.empty:
            self.events.append(SimulationEvent(
                event_id=new_id("se"), simulation_run_id=self.plan.plan_id,
                event_type="DATA_GAP", payload={"reason": "无行情数据"}))
            return self._result()

        close = self.df["close"].astype(float)
        dates = self.df["date"]

        for i in range(len(self.df)):
            date_i = dates.iloc[i]
            # 信号日收盘计算
            sub = self.df.iloc[: i + 1]
            indicator_ctx = self._build_indicator_ctx(sub)
            ctx = self._build_strategy_ctx(sub, indicator_ctx)
            decision = self.strategy.evaluate(ctx)

            if decision.action in {"BUY", "BUY_MORE"} and self._has_next_bar(i):
                self._execute_next_open(i, "BUY", decision)
            elif decision.action in {"SELL_ALL"} and self._has_next_bar(i) and self.account.positions:
                self._execute_next_open(i, "SELL", decision)
            elif decision.action == "SELL_PARTIAL" and self._has_next_bar(i) and self.account.positions:
                self._execute_next_open(i, "SELL_PARTIAL", decision)

            # 每日权益（用当日收盘价估值）
            self._record_equity(i, close.iloc[i], dates.iloc[i])

        self.events.append(SimulationEvent(
            event_id=new_id("se"), simulation_run_id=self.plan.plan_id,
            event_type="END_OF_PERIOD", payload={"end_date": str(dates.iloc[-1])}))
        return self._result()

    # ── 内部 ──────────────────────────────────────────────

    def _has_next_bar(self, i: int) -> bool:
        return i + 1 < len(self.df)

    def _build_indicator_ctx(self, sub: pd.DataFrame):
        if self.registry is None:
            return None
        from StockInvestmentTool.indicators.context import IndicatorContext
        return IndicatorContext(df=sub, registry=self.registry)

    def _build_strategy_ctx(self, sub: pd.DataFrame, indicator_ctx):
        from StockInvestmentTool.biz.models import StrategyContext, DataContext

        pos_qty = sum(p["qty"] for p in self.account.positions.values())
        avg_cost = None
        if pos_qty > 0:
            total = sum(p["qty"] * p["avg_cost"] for p in self.account.positions.values())
            avg_cost = total / pos_qty

        return StrategyContext(
            symbol=self._symbol(),
            evaluation_time=str(sub["date"].iloc[-1]) + "T15:00:00Z",
            data_as_of=str(sub["date"].iloc[-1]),
            market_data=sub,
            indicator_context=indicator_ctx,
            position_state="holding" if pos_qty > 0 else "none",
            position_quantity=pos_qty,
            position_state_avg_cost=avg_cost,
            cash_available=self.account.cash,
            data_context=DataContext(requested_start=str(self.plan.start_date),
                                     requested_end=str(self.plan.end_date)),
        )

    def _symbol(self) -> str:
        return str(self.df["code"].iloc[0]) if "code" in self.df.columns else self.plan.universe_snapshot_id or ""

    def _execute_next_open(self, i: int, side: str, decision) -> None:
        """在 i 日的下一交易日开盘执行。"""
        if not self._has_next_bar(i):
            return
        next_row = self.df.iloc[i + 1]
        exec_price = float(next_row["open"])
        signal_date = str(self.df["date"].iloc[i])
        exec_date = str(next_row["date"])
        signal_price = float(self.df["close"].iloc[i])

        cost = self._cost_config()
        slippage = float(cost.get("slippage", 0.0) or 0.0)
        fee_rate = float(cost.get("fee_rate", 0.001) or 0.001)

        if side in {"BUY", "BUY_MORE"}:
            ratio = decision.quantity_ratio or 0.2
            max_buy = (self.account.cash * ratio) / (exec_price * (1 + slippage))
            qty = self._round_lot(max_buy)
            if qty <= 0:
                self.events.append(SimulationEvent(
                    event_id=new_id("se"), simulation_run_id=self.plan.plan_id,
                    event_type="ORDER_REJECTED", symbol=self._symbol(),
                    payload={"reason": "现金不足" if self.account.cash < exec_price else "数量为0",
                             "exec_price": exec_price}))
                return
            gross = qty * exec_price * (1 + slippage)
            fee = gross * fee_rate
            if gross + fee > self.account.cash:
                # 可买数量按现金约束收缩
                qty = self._round_lot(self.account.cash / (exec_price * (1 + slippage) * (1 + fee_rate)))
                if qty <= 0:
                    return
                gross = qty * exec_price * (1 + slippage)
                fee = gross * fee_rate
            self.account.cash -= (gross + fee)
            pos = self.account.positions.setdefault(self._symbol(), {"qty": 0.0, "avg_cost": 0.0})
            new_qty = pos["qty"] + qty
            pos["avg_cost"] = (pos["avg_cost"] * pos["qty"] + gross) / new_qty if new_qty else 0.0
            pos["qty"] = new_qty
            self.account.total_fees += fee
            fill = SimulationFill(
                fill_id=new_id("fill"), simulation_run_id=self.plan.plan_id,
                symbol=self._symbol(), side="BUY",
                signal_time=signal_date, execution_time=exec_date,
                signal_price=signal_price, execution_price=exec_price,
                quantity=qty, gross_amount=gross, fee=fee, slippage=gross * slippage,
                decision_id=decision.decision_id, reason=decision.reason)
            self.fills.append(fill)
            self.events.append(SimulationEvent(
                event_id=new_id("se"), simulation_run_id=self.plan.plan_id,
                event_type="FILLED", symbol=self._symbol(),
                payload={"side": "BUY", "qty": qty, "price": exec_price}))

        elif side == "SELL":
            pos = self.account.positions.get(self._symbol())
            if not pos or pos["qty"] <= 0:
                return
            qty = pos["qty"]
            gross = qty * exec_price * (1 - slippage)
            fee = gross * fee_rate
            pnl = gross - fee - (pos["avg_cost"] * qty)
            self.account.cash += (gross - fee)
            self.account.total_fees += fee
            pos["qty"] = 0.0
            fill = SimulationFill(
                fill_id=new_id("fill"), simulation_run_id=self.plan.plan_id,
                symbol=self._symbol(), side="SELL",
                signal_time=signal_date, execution_time=exec_date,
                signal_price=signal_price, execution_price=exec_price,
                quantity=qty, gross_amount=gross, fee=fee, slippage=0.0,
                decision_id=decision.decision_id, reason=decision.reason)
            self.fills.append(fill)
            self.events.append(SimulationEvent(
                event_id=new_id("se"), simulation_run_id=self.plan.plan_id,
                event_type="FILLED", symbol=self._symbol(),
                payload={"side": "SELL", "qty": qty, "price": exec_price, "pnl": pnl}))

        elif side == "SELL_PARTIAL":
            pos = self.account.positions.get(self._symbol())
            if not pos or pos["qty"] <= 0:
                return
            ratio = decision.quantity_ratio or 0.5
            qty = self._round_lot(pos["qty"] * ratio)
            if qty <= 0:
                qty = pos["qty"]
            gross = qty * exec_price * (1 - slippage)
            fee = gross * fee_rate
            self.account.cash += (gross - fee)
            self.account.total_fees += fee
            pos["qty"] -= qty
            fill = SimulationFill(
                fill_id=new_id("fill"), simulation_run_id=self.plan.plan_id,
                symbol=self._symbol(), side="SELL",
                signal_time=signal_date, execution_time=exec_date,
                signal_price=signal_price, execution_price=exec_price,
                quantity=qty, gross_amount=gross, fee=fee, slippage=0.0,
                decision_id=decision.decision_id, reason=decision.reason)
            self.fills.append(fill)

    def _round_lot(self, qty: float) -> float:
        """A股 100 股一手取整（向下）。"""
        return float(np.floor(qty / 100.0) * 100)

    def _record_equity(self, i: int, close_price: float, date) -> None:
        pos_qty = sum(p["qty"] for p in self.account.positions.values())
        mv = pos_qty * close_price
        equity = self.account.cash + mv
        self.equity_curve.append({
            "date": str(date),
            "cash": self.account.cash,
            "market_value": mv,
            "equity": equity,
        })

    def _cost_config(self) -> dict:
        return self.plan.cost_config or {}

    # ── 结果 ──────────────────────────────────────────────

    def _result(self) -> SimulationResult:
        initial = self.plan.initial_cash
        final_equity = self.equity_curve[-1]["equity"] if self.equity_curve else initial
        total_return = (final_equity - initial) / initial if initial else 0.0
        max_dd = self._max_drawdown()
        win_rate = self._win_rate()
        profit_factor = self._profit_factor()

        return SimulationResult(
            run_id=self.plan.plan_id,
            initial_cash=initial,
            final_equity=final_equity,
            total_return=total_return,
            benchmark_return=None,
            excess_return=None,
            max_drawdown=max_dd,
            win_rate=win_rate,
            profit_factor=profit_factor,
            trade_count=self._round_trips(),
            fees=self.account.total_fees,
            slippage=self.account.total_slippage,
            equity_curve=self.equity_curve,
            comparison_status="unavailable",
        )

    def _max_drawdown(self) -> float:
        if not self.equity_curve:
            return 0.0
        eq = pd.Series([p["equity"] for p in self.equity_curve])
        peak = eq.cummax()
        dd = (peak - eq) / peak
        return float(dd.max()) if len(dd) else 0.0

    def _win_rate(self) -> float:
        # 统计已平仓的 BUY→SELL 对
        wins = 0
        trades = 0
        buys = [(f.execution_price, f.quantity) for f in self.fills if f.side == "BUY"]
        for f in self.fills:
            if f.side == "SELL" and buys:
                bp = buys.pop(0)
                trades += 1
                if f.execution_price > bp[0]:
                    wins += 1
        return wins / trades if trades else None

    def _profit_factor(self) -> float:
        gross_profit = 0.0
        gross_loss = 0.0
        buys = [(f.execution_price, f.quantity) for f in self.fills if f.side == "BUY"]
        for f in self.fills:
            if f.side == "SELL" and buys:
                bp = buys.pop(0)
                pnl = (f.execution_price - bp[0]) * f.quantity - f.fee
                if pnl > 0:
                    gross_profit += pnl
                else:
                    gross_loss += -pnl
        if gross_loss == 0:
            return gross_profit if gross_profit > 0 else None
        return gross_profit / gross_loss

    def _round_trips(self) -> int:
        buys = sum(1 for f in self.fills if f.side == "BUY")
        sells = sum(1 for f in self.fills if f.side == "SELL")
        return min(buys, sells)


def execute_simulation(plan: SimulationPlan, df: pd.DataFrame, strategy=None,
                       registry=None) -> tuple[SimulationRun, SimulationResult, list, list]:
    """便捷执行入口：返回 (run, result, fills, events)。"""
    run = SimulationRun(run_id=new_id("run"), plan_id=plan.plan_id, status="running")
    executor = SimulationExecutor(plan, df, registry=registry, strategy=strategy)
    result = executor.run()
    run.status = "success"
    run.finished_at = now_utc()
    return run, result, executor.fills, executor.events