from __future__ import annotations

import pandas as pd

from StockInvestmentTool.indicators.engine import IndicatorRegistry
from StockInvestmentTool.strategy.operation_points import OperationPointConfig, calculate


def _with_indicators(df: pd.DataFrame) -> pd.DataFrame:
    reg = IndicatorRegistry()
    computed = reg.compute(df, ["ma5", "ma20", "ma60", "atr14"])
    out = df.copy()
    for name, s in computed.items():
        out[name] = s.values
    return out


def test_buy_signal_can_use_real_open_price():
    dates = pd.date_range("2025-01-01", periods=140, freq="B")
    close = [20.0] * 120 + [19.4] * 18 + [19.2, 19.4]
    frame = _with_indicators(pd.DataFrame({"date": dates, "open": [20.0] * 120 + [19.4] * 18 + [19.3, 19.3],
                          "high": [20.3] * 120 + [21.0] * 20, "low": [19.7] * 120 + [19.0] * 18 + [19.0, 19.3],
                          "close": close, "volume": [1000] * 140,
                          "amount": [100000] * 140}))
    result = calculate(frame, OperationPointConfig(min_rr=0.5))
    assert result.buy_signal is True
