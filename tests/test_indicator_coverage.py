import numpy as np
import pandas as pd

from StockInvestmentTool.indicators.engine import IndicatorRegistry


def test_catalog_technical_indicators_are_registered_and_have_expected_windows():
    dates = pd.date_range("2025-01-01", periods=80, freq="B")
    close = pd.Series(np.linspace(10, 30, len(dates)))
    df = pd.DataFrame({
        "date": dates, "open": close, "high": close + 1, "low": close - 1,
        "close": close, "volume": 1000,
    })

    values = IndicatorRegistry().compute(df, ["rsi14", "macd", "volatility20"])

    assert set(values) == {"rsi14", "macd", "volatility20"}
    assert values["rsi14"].notna().sum() == len(df) - 14
    assert values["macd"].notna().sum() == len(df) - 25
    assert values["volatility20"].notna().sum() == len(df) - 20
    assert values["rsi14"].dropna().between(0, 100).all()
    assert np.isfinite(values["macd"].dropna()).all()
    assert (values["volatility20"].dropna() >= 0).all()
