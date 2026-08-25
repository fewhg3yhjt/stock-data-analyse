"""pytest 共享 fixtures。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from StockInvestmentTool.datasource.indicators import TechnicalIndicators


def make_kline(n: int = 120, base: float = 20.0, start: str = "2024-01-02") -> pd.DataFrame:
    """生成确定性 K 线（匀速上涨，含 ma_* / low_3m / year_low 列）。"""
    dates = pd.bdate_range(start, periods=n)
    close = np.linspace(base, base * 1.5, n)
    df = pd.DataFrame({
        "date": dates,
        "open": close * 0.995,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": np.linspace(1000, 2000, n),
        "amount": np.linspace(1e6, 2e6, n),
        "peTTM": np.linspace(10, 15, n),
        "pbMRQ": np.linspace(1, 1.5, n),
        "turn": np.linspace(0.5, 1.0, n),
    })
    return TechnicalIndicators.compute_all(df)


@pytest.fixture
def kline():
    return make_kline()


@pytest.fixture
def dividends():
    return [
        {"dividSum": "0.5", "pubDate": "2024-06-01"},
        {"dividSum": "0.45", "pubDate": "2023-06-01"},
        {"dividSum": "0.40", "pubDate": "2022-06-01"},
    ]
