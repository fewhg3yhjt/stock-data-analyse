"""V11 is the source of new legacy ActionAdvice projections."""

from __future__ import annotations

import pandas as pd

from StockInvestmentTool.portfolio.manager import PortfolioManager
from StockInvestmentTool.portfolio.models import Position


def test_v11_projection_is_hold_and_marked_source(monkeypatch):
    manager = PortfolioManager.__new__(PortfolioManager)
    position = Position(
        id=7, stock_code="sh.510300", stock_name="测试ETF",
        scheme_name="minute_take_profit_v11", avg_cost=4.0,
        total_shares=800, total_cost=3200, current_price=4.2,
        stop_loss_price=3.5, status="open",
    )
    monkeypatch.setattr(
        "StockInvestmentTool.biz.minute_take_profit_v11.evaluate",
        lambda cycle, current_price: {"state": "HOLD", "notify": False,
                                      "context": {"v11_status": "HOLD", "current_price": current_price}},
    )
    monkeypatch.setattr(
        "StockInvestmentTool.biz.position_runtime._MinuteFirstPriceLoader.latest_price",
        lambda self, symbol: (4.2, "2026-09-07 10:00:00", "minute"),
    )
    advice = manager._v11_advice(position, pd.DataFrame())
    assert advice.advice_type == "hold"
    assert advice.check_results["v11"]["source"] == "minute_take_profit_v11"
    assert "不自动卖出" in advice.reason
