"""V11 must not emit legacy daily left/right take-profit advice."""

from __future__ import annotations

import pandas as pd

from StockInvestmentTool.portfolio.advisor import PostPurchaseAdvisor
from StockInvestmentTool.portfolio.models import Position


def test_v11_advisor_holds_instead_of_year_high_partial_sell():
    position = Position(
        id=1, stock_code="sh.510300", stock_name="测试ETF",
        scheme_name="minute_take_profit_v11", scheme_snapshot={},
        total_shares=800, avg_cost=4.0, total_cost=3200,
        current_price=4.62, peak_price=4.62, status="open",
    )
    dates = pd.bdate_range("2025-01-01", periods=260)
    close = pd.Series([4.0] * 259 + [4.62])
    kline = pd.DataFrame({
        "date": dates, "open": close, "high": close, "low": close,
        "close": close, "volume": 1000.0, "amount": 10000.0,
    })
    advice = PostPurchaseAdvisor().analyze_position(position, kline)
    assert advice.advice_type == "hold"
    assert "分钟级止盈 V11" in advice.reason
    assert "左侧止盈" not in advice.reason
