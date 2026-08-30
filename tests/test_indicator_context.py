"""FR-1.3 指标 × 策略打通（IndicatorContext） 测试。"""

from __future__ import annotations

import pytest

from StockInvestmentTool.indicators.context import IndicatorContext


class TestIndicatorContext:
    def test_getitem_named_indicator(self, kline):
        ctx = IndicatorContext(kline)
        ma20 = ctx["ma20"]
        assert ma20 > 0

    def test_eval_expression(self, kline):
        ctx = IndicatorContext(kline)
        val = ctx.eval("0.95*ma20")
        assert 0 < val
        assert abs(val - 0.95 * ctx["ma20"]) < 1e-6

    def test_eval_min_expression(self, kline):
        ctx = IndicatorContext(kline)
        val = ctx.eval("MIN(ma20,ma60)")
        assert val > 0

    def test_eval_invalid_raises(self, kline):
        ctx = IndicatorContext(kline)
        with pytest.raises(ValueError):
            ctx.eval("0.95*NOT_A_REAL_INDICATOR")

    def test_ma_atomic(self, kline):
        ctx = IndicatorContext(kline)
        assert ctx.ma(20) > 0

    def test_rolling_low_atomic(self, kline):
        ctx = IndicatorContext(kline)
        assert ctx.rolling_low(63) > 0

    def test_year_high(self, kline):
        ctx = IndicatorContext(kline)
        assert ctx.year_high() > 0

    def test_latest_batch(self, kline):
        ctx = IndicatorContext(kline)
        vals = ctx.latest(["ma20", "ma60"])
        assert "ma20" in vals
        assert vals["ma20"] is not None

    def test_config_indicators_loaded(self, kline):
        # indicators.yaml 里定义了 MA17 / MA63（base）+ 止盈参考线(composite)
        ctx = IndicatorContext(kline)
        assert ctx["ma17"] > 0
        assert ctx["ma63"] > 0

    def test_composite_indicator(self, kline):
        ctx = IndicatorContext(kline)
        val = ctx.eval("0.95*MIN(ma20,ma240)")
        assert val >= 0
