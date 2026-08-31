# -*- coding: utf-8 -*-
"""PositionValuationService：唯一持仓估值入口。

依据 docs/ACCOUNT_PORTFOLIO_AND_TRADING_DESIGN.md §8。
- 持仓页、工作台、晨报、收益、复盘、导出和通知不得各自计算收益
- 行情价格必须带 market_price_as_of / price_source
- 数据滞后时不得显示为实时资产
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from StockInvestmentTool.biz.code import normalize
from StockInvestmentTool.biz.models import new_id, now_utc

logger = logging.getLogger(__name__)


@dataclass
class PositionValuation:
    position_cycle_id: str
    symbol: str
    as_of: str
    quantity: float
    average_cost: float | None
    cost_basis: float
    market_price: float | None
    market_value: float
    unrealized_pnl: float | None
    realized_pnl: float
    return_rate: float | None
    market_price_as_of: str | None
    price_source: str = "published_stock_daily"
    data_context: dict = field(default_factory=dict)


class PositionValuationService:
    """持仓估值唯一入口。

    输入：持仓周期信息（quantity/avg_cost/realized_pnl）+ 已发布的日线行情 DataFrame。
    估值只使用 DataFrame 中最后一个交易日收盘价，并返回该价格对应日期。
    """

    def __init__(self, portfolio_service=None, df: pd.DataFrame | None = None,
                 context: dict | None = None):
        if portfolio_service is None:
            from StockInvestmentTool.biz.portfolio import PortfolioService
            portfolio_service = PortfolioService()
        self.pf = portfolio_service
        self.df = df
        self.context = context or {}

    def valuate_cycle(self, position_cycle_id: str) -> PositionValuation:
        summary = self.pf.position_summary(position_cycle_id)
        cycle = self.pf.get_cycle(position_cycle_id)
        if cycle is None:
            raise KeyError(f"unknown position cycle: {position_cycle_id}")

        price, price_date = self._latest_price(cycle.symbol)
        qty = summary["quantity"]
        cost_basis = summary["cost_basis"]
        market_value = qty * price if price is not None else 0.0
        unrealized = (price - summary["average_cost"]) * qty if price is not None and summary["average_cost"] else None
        total_pnl = (unrealized or 0) + summary["realized_pnl"]
        return_rate = total_pnl / cost_basis if cost_basis else None

        return PositionValuation(
            position_cycle_id=position_cycle_id,
            symbol=cycle.symbol,
            as_of=self._as_of(),
            quantity=qty,
            average_cost=summary["average_cost"],
            cost_basis=cost_basis,
            market_price=price,
            market_value=market_value,
            unrealized_pnl=unrealized,
            realized_pnl=summary["realized_pnl"],
            return_rate=return_rate,
            market_price_as_of=price_date,
            price_source="published_stock_daily" if not self.context.get("fallback_used") else "fallback",
            data_context=self.context,
        )

    def valuate_portfolio(self, portfolio_id: str) -> dict:
        """组合总览：现金 + Σ 持仓市值。"""
        cycles = self.pf.list_cycles(portfolio_id)
        valuations = []
        total_mv = 0.0
        total_unrealized = 0.0
        for c in cycles:
            if c.status != "open":
                continue
            v = self.valuate_cycle(c.position_cycle_id)
            valuations.append(v)
            total_mv += v.market_value
            total_unrealized += v.unrealized_pnl or 0.0
        cash = self.pf.cash_balance(portfolio_id)
        return {
            "portfolio_id": portfolio_id,
            "cash_balance": cash,
            "market_value": total_mv,
            "total_assets": cash + total_mv,
            "unrealized_pnl": total_unrealized,
            "valuations": valuations,
            "market_price_as_of": valuations[0].market_price_as_of if valuations else None,
        }

    # ── 内部 ──────────────────────────────────────────────

    def _latest_price(self, symbol: str) -> tuple[float | None, str | None]:
        """从注入的日线 DataFrame 取该 symbol 最后一个交易日收盘价。"""
        if self.df is None or self.df.empty:
            return None, None
        df = self.df
        if "code" in df.columns:
            sym_norm = normalize(symbol)
            codes = df["code"].astype(str)
            # 兼容 code 列可能是带点格式
            try:
                codes_norm = codes.map(lambda c: normalize(c))
                sub = df[codes_norm == sym_norm]
            except Exception:  # noqa: BLE001
                sub = df[codes == symbol]
        else:
            sub = df
        if sub.empty:
            return None, None
        sub = sub.sort_values("date")
        last = sub.iloc[-1]
        price = float(last["close"]) if pd.notna(last["close"]) else None
        price_date = str(pd.to_datetime(last["date"]).strftime("%Y-%m-%d"))
        return price, price_date

    def _as_of(self) -> str:
        if self.df is not None and not self.df.empty and "date" in self.df.columns:
            return str(pd.to_datetime(self.df["date"].iloc[-1]).strftime("%Y-%m-%d"))
        return now_utc()[:10]