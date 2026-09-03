"""Backtest parameter sensitivity regressions."""

from __future__ import annotations

import pandas as pd

from StockInvestmentTool.strategy.take_profit import TakeProfitOptimizer


def test_trail_threshold_is_used_by_right_side_logic(kline):
    optimizer = TakeProfitOptimizer(kline, stock_type="B")
    row = kline.iloc[-1].copy()
    row["close"] = 9.8
    row["high"] = 9.8
    row["low"] = 9.7
    low_threshold = optimizer._run_sell_logic(
        row, 0.01, 0.0, 0.0, 10.0, 100.0, 2, 10.0,
        "right_side", 0, [], False,
    )
    high_threshold = optimizer._run_sell_logic(
        row, 0.50, 0.0, 0.0, 10.0, 100.0, 2, 10.0,
        "right_side", 0, [], False,
    )

    assert low_threshold[-1] != high_threshold[-1] or low_threshold[1] != high_threshold[1]


def test_right_side_trailing_does_not_require_year_high_breakout(kline):
    optimizer = TakeProfitOptimizer(kline, stock_type="B")
    row = kline.iloc[-1].copy()
    row["close"] = 10.0
    row["high"] = 10.0
    row["low"] = 9.9
    trades = []
    result = optimizer._run_sell_logic(
        row, 0.03, 0.0, 0.0, 10.0, 100.0, 2, 10.5,
        "holding", 0, trades, False,
    )
    assert result[-1] is True
    assert trades[0]["type"] == "右侧止盈(移动清仓)"
    assert "突破前高" not in trades[0]["reason"]


def test_scheme_can_disable_technical_stop(kline):
    from StockInvestmentTool.core.scheme import SchemeConfig, RiskConfig
    from StockInvestmentTool.strategy.take_profit import TakeProfitOptimizer

    scheme = SchemeConfig(name="test", risk=RiskConfig(technical_stop_enabled=False))
    optimizer = TakeProfitOptimizer(kline, scheme=scheme)
    assert optimizer.technical_stop_enabled is False
