# -*- coding: utf-8 -*-
"""biz 包单元测试：ResearchService。"""

import numpy as np
import pandas as pd

from StockInvestmentTool.biz.research import ResearchService
from StockInvestmentTool.biz.strategy import StrategySpec, compile_strategy


def make_df():
    n = 20
    close = 10.0 * (1.01 ** np.arange(n))
    df = pd.DataFrame({
        "date": pd.bdate_range("2026-07-01", periods=n),
        "code": "sh600908",
        "open": close * 0.99, "high": close * 1.02, "low": close * 0.98, "close": close,
        "volume": [1000] * n, "amount": [10000] * n,
        "ma20": pd.Series(close).rolling(20, min_periods=1).mean(),
        "ma60": pd.Series(close).rolling(60, min_periods=1).mean(),
        "pe_ttm": [15.0] * n, "pb_mrq": [2.0] * n,
    })
    return df


def make_strategy():
    spec = StrategySpec(
        strategy_id="s1", name="n", version="1",
        entry_rules=[{"rule_id": "b", "action": "BUY", "position_ratio": 0.2, "when": {
            "type": "comparison", "left": {"field": "close"}, "operator": ">", "right": {"value": 10}}}],
        exit_rules=[{"rule_id": "e", "action": "SELL_ALL", "when": {
            "type": "comparison", "left": {"field": "close"}, "operator": "<", "right": {"value": 5}}}],
        position_sizing={"initial_ratio": 0.2},
    )
    return compile_strategy(spec, strategy_version_id="sv_test")


class TestResearch:
    def test_full_research(self):
        ctx = {
            "dataset_refs": {"stock_daily": {
                "partition_versions": {"2026-07": "v1"},
                "quality_status": "PASS",
            }},
            "data_as_of": "2026-07-28",
            "quality_status": "PASS",
        }
        svc = ResearchService(make_df(), ctx, strategy=make_strategy(),
                              market_regime={"regime": "weak_bull", "as_of": "2026-07-28"})
        result = svc.run()
        assert result.status == "success"
        assert result.technical_assessment["status"] == "ok"
        assert result.market_assessment["regime"] == "weak_bull"
        assert result.fundamental_assessment["status"] == "deferred"  # 数据契约未完成
        assert result.strategy_decision_ids
        assert result.evidence_ids

    def test_valuation_degraded(self):
        svc = ResearchService(make_df(), {}, strategy=make_strategy())
        result = svc.run()
        # pe_ttm 存在 → 降级评估
        assert result.valuation_assessment["status"] == "degraded"
        assert result.valuation_assessment["pe_ttm"] == pytest.approx(15.0)

    def test_valuation_unavailable(self):
        df = make_df()
        df = df.drop(columns=["pe_ttm", "pb_mrq"])
        svc = ResearchService(df, {})
        result = svc.run()
        assert result.valuation_assessment["status"] == "unavailable"

    def test_no_strategy_no_decision(self):
        svc = ResearchService(make_df(), {})
        result = svc.run()
        assert not result.strategy_decision_ids
        assert "未提供策略" in " ".join(result.warnings)

    def test_empty_df(self):
        svc = ResearchService(pd.DataFrame(), {})
        result = svc.run()
        assert result.technical_assessment["status"] == "unavailable"


import pytest  # noqa: E402
