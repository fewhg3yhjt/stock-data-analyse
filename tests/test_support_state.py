"""FR-1.2 支撑位骨架 / 状态机 去重 测试。"""

from __future__ import annotations

import pytest

from StockInvestmentTool.indicators.context import IndicatorContext
from StockInvestmentTool.strategy.position_state import PositionStateMachine
from StockInvestmentTool.strategy.support import (
    IndicatorExprSource,
    MaSource,
    RollingLowSource,
    build_sources,
    get_support_levels,
)


class TestSupport:
    def test_skeleton_orders_candidates(self, kline):
        ctx = IndicatorContext(kline)
        sources = [RollingLowSource(63), MaSource(20), MaSource(60)]
        weak, strong, extreme = get_support_levels(sources, ctx, kline.iloc[-1])
        assert strong <= weak            # 强支撑 <= 弱支撑
        assert extreme == strong         # 无股息锚 → extreme = strong

    def test_dividend_anchor_extreme(self, kline):
        ctx = IndicatorContext(kline)
        sources = [MaSource(20), MaSource(60)]
        weak, strong, extreme = get_support_levels(sources, ctx, kline.iloc[-1],
                                                   dividend_anchor=9.0)
        assert extreme == 9.0

    def test_no_candidates_returns_zero(self, kline):
        # 构造全 NaN 的上下文
        ctx = (lambda df: IndicatorContext(df))(kline)
        # 用一个总是返回 0 的来源
        res = get_support_levels([lambda *a, **k: 0.0], ctx, kline.iloc[-1])
        assert res == (0.0, 0.0, 0.0)

    def test_expr_source_from_indicator(self, kline):
        ctx = IndicatorContext(kline)
        src = IndicatorExprSource("0.95*ma20")
        v = src.compute(ctx, kline.iloc[-1])
        assert v > 0

    def test_source_factory_compat_mapping(self, kline):
        ctx = IndicatorContext(kline)
        sources, anchor = build_sources(["ma60", "low_3m", "year_low"])
        assert len(sources) == 3
        weak, strong, extreme = get_support_levels(sources, ctx, kline.iloc[-1])
        assert strong <= weak

    def test_source_factory_expression(self, kline):
        ctx = IndicatorContext(kline)
        sources, _ = build_sources(["ma60", "MIN(ma20,ma240)"])
        assert len(sources) == 2
        assert isinstance(sources[0], MaSource)
        assert isinstance(sources[1], IndicatorExprSource)

    def test_dividend_anchor_not_in_candidates(self, kline):
        # dividend_anchor 只作 extreme，不进强/弱候选
        sources, anchor = build_sources(["ma60", "dividend_anchor"])
        assert len(sources) == 1
        assert isinstance(sources[0], MaSource)


class TestPositionStateMachine:
    def test_valid_transitions(self):
        sm = PositionStateMachine()
        assert sm.transition("accumulating", "bought") == "holding"
        assert sm.transition("accumulating", "left_tp") == "left_side"
        assert sm.transition("holding", "breakout") == "right_side"
        assert sm.transition("left_side", "stop") == "closed"
        assert sm.transition("closed", "reopen") == "accumulating"

    def test_invalid_transition_raises(self):
        sm = PositionStateMachine()
        with pytest.raises(ValueError):
            sm.transition("holding", "reopen")   # 未平仓不可 reopen

    def test_can_and_next_states(self):
        sm = PositionStateMachine()
        assert sm.can("accumulating", "bought")
        assert not sm.can("closed", "breakout")
        evs = sm.next_states("holding")
        assert set(evs) >= {"left_tp", "breakout", "stop"}

    def test_event_for_action(self):
        assert PositionStateMachine.event_for("clear") == "closed"
        assert PositionStateMachine.event_for("partial_sell") == "left_tp"
        assert PositionStateMachine.event_for("transition") == "breakout"
        assert PositionStateMachine.event_for("hold") == ""
