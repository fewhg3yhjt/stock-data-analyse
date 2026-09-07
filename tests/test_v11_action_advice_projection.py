"""V11 is evaluated by PositionRuntimeService, not legacy ActionAdvice."""

from __future__ import annotations


def test_v11_has_no_legacy_manager_entrypoint():
    from StockInvestmentTool.portfolio.manager import PortfolioManager

    assert not hasattr(PortfolioManager, "_v11_advice")
    assert not hasattr(PortfolioManager, "refresh_v11_minute_positions")
