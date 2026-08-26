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
