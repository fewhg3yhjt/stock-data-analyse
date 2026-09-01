# -*- coding: utf-8 -*-
"""biz 包单元测试：ScreenExecutor / ConditionCompiler。"""

import numpy as np
import pandas as pd
import pytest

from StockInvestmentTool.biz.screen import (
    ConditionCompiler,
    ScreenDefinition,
    ScreenExecutor,
)


def make_market_df():
    """构造 3 只股票 × 5 日行情，sh600908 收盘最高。"""
    symbols = ["sh600908", "sz000001", "sh601211"]
    frames = []
    for si, sym in enumerate(symbols):
        n = 5
        base = [10, 12, 15][si]
        close = np.array([base, base + 0.1, base + 0.2, base + 0.1, base + 0.5])
        df = pd.DataFrame({
            "date": pd.bdate_range("2026-08-01", periods=n),
            "code": sym,
            "open": close * 0.99, "high": close * 1.02, "low": close * 0.98, "close": close,
            "volume": [1000] * n, "amount": [10000] * n,
        })
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


class TestConditionCompiler:
    def test_modes(self):
        compiler = ConditionCompiler({
            "type": "and",
            "conditions": [
                {"type": "comparison", "left": {"field": "close"}, "operator": ">", "right": {"value": 10}},
                {"type": "cross", "left": {"indicator": "ma5"}, "direction": "above", "right": {"indicator": "ma20"}},
            ],
        })
        modes = compiler.compile_mode()
        assert modes["mode"] == "exact_only"
        assert modes["children"][0]["mode"] == "conservative_sql"
        assert modes["children"][1]["mode"] == "exact_only"

    def test_dependencies(self):
        compiler = ConditionCompiler({
            "type": "comparison", "left": {"field": "close"}, "operator": ">", "right": {"indicator": "ma60"},
        })
        assert set(compiler.dependencies) == {"close", "ma60"}


class TestScreenExecutor:
    def test_selects_matching(self):
        spec = {
            "type": "comparison", "left": {"field": "close"}, "operator": ">", "right": {"value": 12},
        }
        definition = ScreenDefinition(
            screen_id="sc1", name="高价股", version="1",
            condition_spec=spec,
            sort_spec={"field": "symbol", "direction": "asc"},
        )
        executor = ScreenExecutor(definition, make_market_df())
        candidates, meta = executor.execute(as_of="2026-08-07")
        assert meta["status"] == "success"
        symbols = [c.symbol for c in candidates]
        # 条件 close > 12：10.5 不命中，12.5/15.5 命中
        assert "sh600908" not in symbols
        assert "sh601211" in symbols
        assert "sz000001" in symbols
        for c in candidates:
            assert c.data_as_of == "2026-08-07"
            assert "explanation" in c.condition_results

    def test_rank_and_sort(self):
        spec = {
            "type": "comparison", "left": {"field": "close"}, "operator": ">", "right": {"value": 10},
        }
        definition = ScreenDefinition(
            screen_id="sc2", name="全部", version="1", condition_spec=spec,
            sort_spec={"field": "symbol", "direction": "desc"},
        )
        executor = ScreenExecutor(definition, make_market_df())
        candidates, _ = executor.execute(as_of="2026-08-07")
        assert len(candidates) == 3
        assert [c.rank_no for c in candidates] == [1, 2, 3]
        # 降序
        assert candidates[0].symbol > candidates[-1].symbol

    def test_no_match_returns_empty(self):
        spec = {
            "type": "comparison", "left": {"field": "close"}, "operator": ">", "right": {"value": 100},
        }
        definition = ScreenDefinition(screen_id="sc3", name="无", version="1", condition_spec=spec)
        executor = ScreenExecutor(definition, make_market_df())
        candidates, meta = executor.execute(as_of="2026-08-07")
        assert candidates == []
        assert meta["matched_count"] == 0
        assert meta["status"] == "success"

    def test_historical_as_of(self):
        spec = {
            "type": "comparison", "left": {"field": "close"}, "operator": ">", "right": {"value": 10},
        }
        definition = ScreenDefinition(screen_id="sc4", name="历史", version="1", condition_spec=spec)
        executor = ScreenExecutor(definition, make_market_df())
        # as_of 落在第 3 天，只应评估前 3 天
        candidates, meta = executor.execute(as_of="2026-08-03")
        assert meta["actual_data_as_of"] == "2026-08-03"

    def test_compile_modes_in_meta(self):
        spec = {
            "type": "and",
            "conditions": [
                {"type": "comparison", "left": {"field": "close"}, "operator": ">", "right": {"value": 10}},
                {"type": "cross", "left": {"indicator": "ma5"}, "direction": "above", "right": {"indicator": "ma20"}},
            ],
        }
        definition = ScreenDefinition(screen_id="sc5", name="组合", version="1", condition_spec=spec)
        executor = ScreenExecutor(definition, make_market_df())
        _, meta = executor.execute(as_of="2026-08-07")
        assert meta["compile_modes"]["type"] == "and"

    def test_signal_scan_deduplicates_symbols(self):
        spec = {
            "type": "comparison", "left": {"field": "close"},
            "operator": ">", "right": {"value": 10},
        }
        definition = ScreenDefinition(screen_id="sc6", name="区间", condition_spec=spec)
        candidates, meta = ScreenExecutor(definition, make_market_df()).execute(
            as_of="2026-08-07", execution_mode="signal_scan",
            scan_start="2026-08-03", scan_end="2026-08-07",
        )
        assert meta["execution_mode"] == "signal_scan"
        assert len(candidates) == len({candidate.symbol for candidate in candidates})
        assert {candidate.symbol for candidate in candidates} == {"sh600908", "sz000001", "sh601211"}
        assert all(candidate.signal_count >= 1 for candidate in candidates)
        assert all(candidate.first_signal_date and candidate.last_signal_date for candidate in candidates)

    def test_signal_scan_rejects_window_over_31_days(self):
        definition = ScreenDefinition(
            screen_id="sc7", name="超长", condition_spec={
                "type": "comparison", "left": {"field": "close"},
                "operator": ">", "right": {"value": 10},
            },
        )
        with pytest.raises(ValueError, match="31"):
            ScreenExecutor(definition, make_market_df()).execute(
                as_of="2026-08-31", execution_mode="signal_scan",
                scan_start="2026-07-01", scan_end="2026-08-31",
            )
