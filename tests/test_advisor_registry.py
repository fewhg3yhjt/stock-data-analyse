"""Production advisor paths must use registry-backed rule results."""

from __future__ import annotations

from StockInvestmentTool.portfolio.advisor import PostPurchaseAdvisor
from StockInvestmentTool.portfolio.models import Position


def test_hard_stop_trigger_has_no_legacy_rate_name_error(kline):
    position = Position(
        id=1, stock_code="sh600900", stock_name="测试股", stock_type="B",
        total_shares=100, avg_cost=20, total_cost=2000,
        current_price=16, peak_price=20, status="open",
        position_phase="holding", scheme_name="default_value",
    )
    frame = kline.copy()
    frame.loc[frame.index[-1], "low"] = 16.0
    frame.loc[frame.index[-1], "close"] = 16.0
    advice = PostPurchaseAdvisor().analyze_position(position, frame)

    assert advice.advice_type == "sell_all"
    assert "止损线" in advice.reason
