from __future__ import annotations

import pandas as pd

from StockInvestmentTool.portfolio.trade_metrics import calculate_post_metrics


def test_post_metrics_excludes_buy_day_and_merges_minute_first():
    daily = pd.DataFrame({
        "date": ["2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"],
        "high": [15, 12, 30, 20], "low": [8, 9, 10, 11],
    })
    minute = pd.DataFrame({
        "trade_date": ["2026-08-22", "2026-08-22"],
        "time": ["2026-08-22 10:00:00", "2026-08-22 14:00:00"],
        "close": [18, 25],
    })
    result = calculate_post_metrics(
        buy_date="2026-08-20", buy_price=10, daily=daily, minute=minute,
        as_of="2026-08-23 15:00:00",
    )
    assert result.post_high == 25
    assert result.post_low == 9
    assert result.post_high_source == "minute"
    assert "2026-08-22" in result.covered_minute_days
    assert "2026-08-21" in result.daily_fallback_days
    assert "2026-08-20" not in result.daily_fallback_days


def test_post_metrics_buy_price_is_boundary():
    daily = pd.DataFrame({"date": ["2026-08-21"], "high": [9], "low": [8]})
    result = calculate_post_metrics(buy_date="2026-08-20", buy_price=10, daily=daily)
    assert result.post_high == 10
    assert result.post_low == 8
