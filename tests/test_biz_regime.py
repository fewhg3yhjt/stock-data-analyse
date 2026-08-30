# -*- coding: utf-8 -*-
"""biz 包单元测试：MarketRegimeService。"""

import numpy as np
import pandas as pd

from StockInvestmentTool.biz.regime import ALGORITHM_VERSION, MarketRegimeService, REGIMES


def make_index_df(trend="up"):
    """构造指数行情。trend: up/down/flat。"""
    n = 80
    if trend == "up":
        close = 3000.0 * (1.002 ** np.arange(n))
    elif trend == "down":
        close = 3000.0 * (0.998 ** np.arange(n))
    else:
        close = np.full(n, 3000.0)
    df = pd.DataFrame({
        "date": pd.bdate_range("2026-04-01", periods=n),
        "code": "sh000300",
        "open": close * 0.999, "high": close * 1.001, "low": close * 0.999, "close": close,
        "volume": [1000] * n, "amount": [100000] * n,
    })
    return df


class TestMarketRegime:
    def test_strong_bull(self):
        svc = MarketRegimeService(make_index_df("up"))
        r = svc.compute("2026-07-22")
        assert r.regime in REGIMES
        assert r.algorithm_version == ALGORITHM_VERSION
        assert r.confidence is not None
        assert r.input_snapshot["algorithm_version"] == ALGORITHM_VERSION
        assert r.explanation

    def test_strong_bear(self):
        svc = MarketRegimeService(make_index_df("down"))
        r = svc.compute("2026-07-22")
        assert r.regime in {"weak_bear", "strong_bear"}

    def test_flat_is_range(self):
        svc = MarketRegimeService(make_index_df("flat"))
        r = svc.compute("2026-07-22")
        assert r.regime == "range"

    def test_empty_returns_range(self):
        svc = MarketRegimeService(pd.DataFrame())
        r = svc.compute("2026-07-22")
        assert r.regime == "range"

    def test_deterministic(self):
        svc = MarketRegimeService(make_index_df("up"))
        r1 = svc.compute("2026-07-22")
        r2 = svc.compute("2026-07-22")
        assert r1.regime == r2.regime
        assert r1.input_snapshot == r2.input_snapshot

    def test_valid_regime(self):
        assert MarketRegimeService.valid_regime("weak_bull")
        assert not MarketRegimeService.valid_regime("banana")