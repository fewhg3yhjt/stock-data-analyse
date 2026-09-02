import pandas as pd

from portfolio.position_levels import (
    calculate_position_drawdown,
    calculate_right_side_trigger_price,
    calculate_year_high,
)


def test_year_high_uses_configured_window_and_field():
    frame = pd.DataFrame({
        "high": [10, 12, 11, 15],
        "close": [9, 11, 10, 14],
    })
    assert calculate_year_high(frame, window=2, price_field="high") == 15
    assert calculate_year_high(frame, window=3, price_field="close") == 14


def test_position_drawdown_and_right_side_trigger_price():
    assert calculate_position_drawdown(20, 19) == 0.05
    assert calculate_right_side_trigger_price(20, 0.05) == 19.0
    assert calculate_position_drawdown(20, 21) == -0.05
