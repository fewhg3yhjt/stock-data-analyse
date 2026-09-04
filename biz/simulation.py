# -*- coding: utf-8 -*-
"""统一回测/模拟内核。

模拟成交使用单边 ``SimulationFill``，持仓使用内存 Lot 并按 FIFO 消耗。
所有收益指标都从同一批 fills/lots 推导，避免平均成本与 FIFO 并存。
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
class SimulationLotState:
    lot_id: str
    symbol: str
    opened_at: str
    quantity: float
    remaining_quantity: float
    entry_price: float
    entry_fee: float
    source_fill_id: str


@dataclass
class ClosedTrade:
    symbol: str
    quantity: float
    proceeds: float
    cost: float
    fees: float
    tax: float
    pnl: float
    opened_at: str
    closed_at: str


@dataclass
class SimulationAccount:
    cash: float
    positions: dict[str, list[SimulationLotState]] = field(default_factory=dict)
    equity: float = 0.0
    total_fees: float = 0.0
    total_tax: float = 0.0
    total_slippage: float = 0.0
    closed_trades: list[ClosedTrade] = field(default_factory=list)


class SimulationExecutor:
    """按交易日推进一个或多个标的的模拟账户。"""

    def __init__(self, plan: SimulationPlan, df: pd.DataFrame,
                 registry: Any = None, strategy=None, run_id: str | None = None):
        self.plan = plan
        self.run_id = run_id or plan.plan_id
        self.df = self._prepare(df)
        self.registry = registry
        self.strategy = strategy
        self.fills: list[SimulationFill] = []
        self.events: list[SimulationEvent] = []
        self.account = SimulationAccount(cash=plan.initial_cash)
        self.equity_curve: list[dict] = []

    @staticmethod
    def _prepare(df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()
        if "date" in result.columns:
            result["date"] = pd.to_datetime(result["date"])
        if "code" not in result.columns:
            raise ValueError("simulation data requires code column")
        return result.sort_values(["date", "code"]).reset_index(drop=True)

    def run(self) -> SimulationResult:
        if self.strategy is None:
            raise ValueError("SimulationExecutor 需要 CompiledStrategy")
        if self.df.empty:
            self._event("DATA_GAP", payload={"reason": "无行情数据"})
            return self._result()

        dates = sorted(self.df["date"].dropna().unique())
        symbols = sorted(self.df["code"].astype(str).unique())
        for date in dates:
            date_text = pd.Timestamp(date).strftime("%Y-%m-%d")
            for symbol in symbols:
                history = self.df[(self.df["code"].astype(str) == symbol)
                                  & (self.df["date"] <= date)]
                if history.empty:
                    continue
                current = history.iloc[-1]
                indicator_ctx = self._build_indicator_ctx(history)
                context = self._build_strategy_ctx(symbol, history, indicator_ctx)
                decision = self.strategy.evaluate(context)
                row_index = self._row_index(symbol, date)
                if decision.action in {"BUY", "BUY_MORE"} and row_index is not None:
                    if self._entry_uses_limit_range():
                        self._execute_next_day_limit(symbol, row_index, "BUY", decision)
                    else:
                        self._execute_next_open(symbol, row_index, "BUY", decision)
                elif decision.action == "SELL_ALL" and row_index is not None:
                    self._execute_next_open(symbol, row_index, "SELL", decision)
                elif decision.action == "SELL_PARTIAL" and row_index is not None:
                    self._execute_next_open(symbol, row_index, "SELL_PARTIAL", decision)
            self._record_equity(date)

        self._event("END_OF_PERIOD", payload={"end_date": pd.Timestamp(dates[-1]).strftime("%Y-%m-%d")})
        return self._result()

    def _entry_uses_limit_range(self) -> bool:
        """Whether buy signals are valid only when touched during T+1."""

        rules = self.plan.execution_rules or {}
        return rules.get("entry_mode", rules.get("mode", "next_open")) == "next_day_limit_range"

    def _row_index(self, symbol: str, date) -> int | None:
        rows = self.df[(self.df["code"].astype(str) == symbol) & (self.df["date"] == date)]
        if rows.empty:
            return None
        return int(rows.index[0])

    def _build_indicator_ctx(self, history: pd.DataFrame):
        from StockInvestmentTool.indicators.context import IndicatorContext
        from StockInvestmentTool.indicators.engine import IndicatorRegistry
        return IndicatorContext(df=history.reset_index(drop=True), registry=self.registry or IndicatorRegistry())

    def _build_strategy_ctx(self, symbol: str, history: pd.DataFrame, indicator_ctx):
        from StockInvestmentTool.biz.models import DataContext, StrategyContext
        lots = self.account.positions.get(symbol, [])
        quantity = sum(lot.remaining_quantity for lot in lots)
        avg_cost = self._average_cost(lots)
        as_of = pd.Timestamp(history["date"].iloc[-1]).strftime("%Y-%m-%d")
        return StrategyContext(
            symbol=symbol, evaluation_time=f"{as_of}T15:00:00Z", data_as_of=as_of,
            market_data=history.reset_index(drop=True), indicator_context=indicator_ctx,
            position_state="holding" if quantity > 0 else "none",
            position_quantity=quantity, position_state_avg_cost=avg_cost,
            cash_available=self.account.cash,
            data_context=DataContext(
                requested_start=str(self.plan.start_date),
                requested_end=str(self.plan.end_date),
                returned_start=str(history["date"].min())[:10] if not history.empty else None,
                returned_end=as_of,
                data_as_of=as_of,
            ),
        )

    def _execute_next_open(self, symbol: str, row_index: int, side: str, decision) -> None:
        rows = self.df[self.df["code"].astype(str) == symbol]
        positions = rows.index.tolist()
        try:
            pos = positions.index(row_index)
        except ValueError:
            return
        if pos + 1 >= len(rows):
            return
        next_row = rows.iloc[pos + 1]
        exec_price = float(next_row["open"])
        signal_row = rows.iloc[pos]
        signal_date = pd.Timestamp(signal_row["date"]).strftime("%Y-%m-%d")
        exec_date = pd.Timestamp(next_row["date"]).strftime("%Y-%m-%d")
        signal_price = float(signal_row["close"])
        costs = self.plan.cost_config or {}
        slippage_rate = float(costs.get("slippage", 0.0) or 0.0)
        fee_rate = float(costs.get("fee_rate", costs.get("commission_rate", 0.001)) or 0.0)
        stamp_tax_rate = float(costs.get("stamp_tax_rate", 0.0) or 0.0)

        if side == "BUY":
            signal_close = float(signal_row["close"])
            qty = self._buy_quantity(symbol, signal_close, exec_price,
                                     decision.quantity_ratio or 0.2,
                                     slippage_rate, fee_rate)
            if qty <= 0:
                self._event("ORDER_REJECTED", symbol, {"reason": "现金或最大仓位不足"})
                return
            effective = exec_price * (1 + slippage_rate)
            gross = qty * effective
            fee = gross * fee_rate
            self.account.cash -= gross + fee
            self.account.total_fees += fee
            slip = qty * exec_price * slippage_rate
            self.account.total_slippage += slip
            fill = SimulationFill(
                fill_id=new_id("fill"), simulation_run_id=self.run_id, symbol=symbol,
                side="BUY", signal_time=signal_date, execution_time=exec_date,
                signal_price=signal_price, execution_price=effective, quantity=qty,
                gross_amount=gross, fee=fee, tax=0.0, slippage=slip,
                decision_id=decision.decision_id, reason=decision.reason,
            )
            self.fills.append(fill)
            self.account.positions.setdefault(symbol, []).append(SimulationLotState(
                lot_id=new_id("slot"), symbol=symbol, opened_at=exec_date,
                quantity=qty, remaining_quantity=qty, entry_price=effective,
                entry_fee=fee, source_fill_id=fill.fill_id,
            ))
            self._event("FILLED", symbol, {"side": "BUY", "quantity": qty, "price": effective})
            return

        lots = self.account.positions.get(symbol, [])
        quantity = sum(lot.remaining_quantity for lot in lots)
        if quantity <= 0:
            return
        if side == "SELL_PARTIAL":
            quantity = min(quantity, self._round_lot(quantity * (decision.quantity_ratio or 0.5)))
            if quantity <= 0:
                quantity = sum(lot.remaining_quantity for lot in lots)
        effective = exec_price * (1 - slippage_rate)
        gross = quantity * effective
        fee = gross * fee_rate
        tax = gross * stamp_tax_rate
        proceeds = gross
        self.account.cash += proceeds
        self.account.total_fees += fee
        self.account.total_tax += tax
        slip = quantity * exec_price * slippage_rate
        self.account.total_slippage += slip
        remaining = quantity
        cost = 0.0
        entry_fees = 0.0
        opened_at = exec_date
        for lot in lots:
            if remaining <= 0:
                break
            consume = min(remaining, lot.remaining_quantity)
            cost += consume * lot.entry_price
            entry_fees += lot.entry_fee * consume / lot.quantity if lot.quantity else 0.0
            opened_at = lot.opened_at
            lot.remaining_quantity -= consume
            remaining -= consume
        allocated_exit_costs = fee + tax
        pnl = proceeds - cost - entry_fees - fee - tax
        self.account.closed_trades.append(ClosedTrade(
            symbol=symbol, quantity=quantity, proceeds=proceeds,
            cost=cost, fees=entry_fees + fee, tax=tax, pnl=pnl,
            opened_at=opened_at, closed_at=exec_date,
        ))
        # Keep exhausted lots for a complete, auditable run history; valuation
        # and future order checks use remaining_quantity only.
        self.account.positions[symbol] = lots
        fill = SimulationFill(
            fill_id=new_id("fill"), simulation_run_id=self.run_id, symbol=symbol,
            side="SELL", signal_time=signal_date, execution_time=exec_date,
            signal_price=signal_price, execution_price=effective, quantity=quantity,
            gross_amount=gross, fee=fee, tax=tax, slippage=slip,
            decision_id=decision.decision_id, reason=decision.reason,
        )
        self.fills.append(fill)
        event = "STOP_TRIGGERED" if "止损" in decision.reason else "TAKE_PROFIT_TRIGGERED" if "止盈" in decision.reason else "FILLED"
        self._event(event, symbol, {"side": "SELL", "quantity": quantity, "price": effective, "pnl": pnl})

    def _execute_next_day_limit(self, symbol: str, row_index: int, side: str, decision) -> None:
        """Execute a next-day limit order using only the next bar's OHLC.

        The signal is created after T closes.  If the next bar opens at or
        below a buy limit, the order receives the better open price.  If it
        opens above the limit, it fills only when the intraday range reaches
        the limit.  No next-day close or volume is consulted.
        """

        if side != "BUY":
            raise ValueError("next_day_limit_range currently supports BUY entries only")
        limit_price = getattr(decision, "price", None)
        if limit_price is None or float(limit_price) <= 0:
            self._event("ORDER_REJECTED", symbol, {"reason": "限价策略缺少有效限价"})
            return
        rows = self.df[self.df["code"].astype(str) == symbol]
        positions = rows.index.tolist()
        try:
            pos = positions.index(row_index)
        except ValueError:
            return
        if pos + 1 >= len(rows):
            return
        signal_row = rows.iloc[pos]
        next_row = rows.iloc[pos + 1]
        limit = float(limit_price)
        open_price = float(next_row["open"])
        low = float(next_row["low"])
        high = float(next_row["high"])
        if low <= open_price <= limit:
            execution_price = open_price
            execution_type = "limit_open_improvement"
        elif low <= limit <= high:
            execution_price = limit
            execution_type = "limit_range_touch"
        else:
            self._event("ORDER_NOT_FILLED", symbol, {
                "side": "BUY", "limit_price": limit,
                "order_date": pd.Timestamp(next_row["date"]).strftime("%Y-%m-%d"),
                "open": open_price, "high": high, "low": low,
                "reason": "次日盘中区间未触及限价",
            })
            return

        costs = self.plan.cost_config or {}
        slippage_rate = float(costs.get("slippage", 0.0) or 0.0)
        fee_rate = float(costs.get("fee_rate", costs.get("commission_rate", 0.001)) or 0.0)
        signal_date = pd.Timestamp(signal_row["date"]).strftime("%Y-%m-%d")
        exec_date = pd.Timestamp(next_row["date"]).strftime("%Y-%m-%d")
        signal_price = float(signal_row["close"])
        qty = self._buy_quantity(symbol, signal_price, execution_price,
                                 decision.quantity_ratio or 0.2,
                                 slippage_rate, fee_rate)
        if qty <= 0:
            self._event("ORDER_REJECTED", symbol, {"reason": "现金或最大仓位不足"})
            return
        effective = execution_price * (1 + slippage_rate)
        gross = qty * effective
        fee = gross * fee_rate
        self.account.cash -= gross + fee
        self.account.total_fees += fee
        slip = qty * execution_price * slippage_rate
        self.account.total_slippage += slip
        fill = SimulationFill(
            fill_id=new_id("fill"), simulation_run_id=self.run_id, symbol=symbol,
            side="BUY", signal_time=signal_date, execution_time=exec_date,
            signal_price=signal_price, execution_price=effective, quantity=qty,
            gross_amount=gross, fee=fee, tax=0.0, slippage=slip,
            decision_id=decision.decision_id, reason=decision.reason,
        )
        self.fills.append(fill)
        self.account.positions.setdefault(symbol, []).append(SimulationLotState(
            lot_id=new_id("slot"), symbol=symbol, opened_at=exec_date,
            quantity=qty, remaining_quantity=qty, entry_price=effective,
            entry_fee=fee, source_fill_id=fill.fill_id,
        ))
        self._event("FILLED", symbol, {
            "side": "BUY", "quantity": qty, "price": effective,
            "limit_price": limit, "execution_type": execution_type,
        })

    def _buy_quantity(self, symbol: str, signal_price: float, execution_price: float, ratio: float,
                      slippage: float, fee_rate: float) -> float:
        current_value = sum(lot.remaining_quantity for lot in self.account.positions.get(symbol, [])) * signal_price
        equity = self.account.cash + current_value
        max_position = float((self.plan.position_sizing or {}).get(
            "max_position_ratio", (self.strategy.spec.risk if self.strategy else {}).get("max_position_ratio", 1.0)))
        budget = min(self.account.cash * max(float(ratio), 0.0),
                     max(0.0, equity * max_position - current_value))
        unit_cost = execution_price * (1 + slippage) * (1 + fee_rate)
        return self._round_lot(budget / unit_cost if unit_cost else 0.0)

    @staticmethod
    def _average_cost(lots: list[SimulationLotState]) -> float | None:
        quantity = sum(lot.remaining_quantity for lot in lots)
        if quantity <= 0:
            return None
        return sum(lot.remaining_quantity * lot.entry_price for lot in lots) / quantity

    @staticmethod
    def _round_lot(quantity: float) -> float:
        return float(np.floor(max(quantity, 0.0) / 100.0) * 100)

    def _record_equity(self, date) -> None:
        date_text = pd.Timestamp(date).strftime("%Y-%m-%d")
        day = self.df[self.df["date"] == date]
        market_value = 0.0
        for symbol, lots in self.account.positions.items():
            rows = day[day["code"].astype(str) == symbol]
            if not rows.empty and pd.notna(rows.iloc[-1]["close"]):
                market_value += sum(lot.remaining_quantity for lot in lots) * float(rows.iloc[-1]["close"])
        self.account.equity = self.account.cash + market_value
        self.equity_curve.append({"date": date_text, "cash": self.account.cash,
                                  "market_value": market_value, "equity": self.account.equity})

    def _event(self, event_type: str, symbol: str = "", payload: dict | None = None) -> None:
        self.events.append(SimulationEvent(
            event_id=new_id("se"), simulation_run_id=self.run_id,
            event_type=event_type, symbol=symbol, payload=payload or {},
        ))

    def _result(self) -> SimulationResult:
        initial = self.plan.initial_cash
        final = self.equity_curve[-1]["equity"] if self.equity_curve else initial
        total_return = (final - initial) / initial if initial else 0.0
        trades = self.account.closed_trades
        wins = [trade for trade in trades if trade.pnl > 0]
        losses = [trade for trade in trades if trade.pnl < 0]
        gross_profit = sum(trade.pnl for trade in wins)
        gross_loss = -sum(trade.pnl for trade in losses)
        profit_factor = gross_profit / gross_loss if gross_loss else (gross_profit if gross_profit else None)
        return SimulationResult(
            run_id=self.run_id, initial_cash=initial, final_equity=final,
            total_return=total_return, max_drawdown=self._max_drawdown(),
            win_rate=len(wins) / len(trades) if trades else None,
            profit_factor=profit_factor, trade_count=len(trades),
            fees=self.account.total_fees, slippage=self.account.total_slippage,
            equity_curve=self.equity_curve, comparison_status="unavailable",
        )

    def _max_drawdown(self) -> float:
        if not self.equity_curve:
            return 0.0
        values = pd.Series([point["equity"] for point in self.equity_curve])
        peak = values.cummax()
        return float(((peak - values) / peak).max())


def execute_simulation(plan: SimulationPlan, df: pd.DataFrame, strategy=None,
                       registry=None) -> tuple[SimulationRun, SimulationResult, list, list]:
    run = SimulationRun(run_id=new_id("run"), plan_id=plan.plan_id, status="running")
    executor = SimulationExecutor(plan, df, registry=registry, strategy=strategy, run_id=run.run_id)
    result = executor.run()
    run.status = "success"
    run.finished_at = now_utc()
    return run, result, executor.fills, executor.events
