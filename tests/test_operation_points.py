from __future__ import annotations

import pandas as pd
import pytest

from StockInvestmentTool.indicators.engine import IndicatorRegistry
from StockInvestmentTool.strategy.operation_points import OperationPointConfig, add_indicators, calculate


def _with_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """为日线帧附上统一指标列（ma5/ma20/ma60/atr14），模拟 indicators 分区输出。"""
    reg = IndicatorRegistry()
    computed = reg.compute(df, ["ma5", "ma20", "ma60", "atr14"])
    out = df.copy()
    for name, s in computed.items():
        out[name] = s.values
    return out


def _frame(n=140):
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    close = [20 + ((i % 12) - 6) * 0.15 for i in range(n)]
    return _with_indicators(pd.DataFrame({"date": dates, "open": close, "high": [x + .4 for x in close],
                         "low": [x - .4 for x in close], "close": close,
                         "volume": [1000 + i for i in range(n)],
                         "amount": [100000 + i for i in range(n)]}))


def test_operation_point_config_is_adjustable_and_validated():
    config = OperationPointConfig.from_dict({"lookback_days": 25,
        "low_position_threshold": .3, "ma_slope_threshold": .025,
        "center_shift_threshold": .04, "ma_distance_threshold": .05,
        "ma20_cross_count_threshold": 4, "stop_atr_k": .5, "min_rr": 2.5})
    assert config.lookback_days == 25
    assert config.low_position_threshold == .3
    assert config.stop_atr_k == .5
    with pytest.raises(ValueError):
        OperationPointConfig.from_dict({"min_rr": 0})


def test_operation_point_result_contains_p0_fields():
    config = OperationPointConfig()
    enriched = add_indicators(_frame(), config)
    result = calculate(enriched, config)
    assert result.strategy["strategy_type"] == "operation_points_v1"
    assert result.h20 is not None and result.l20 is not None
    assert result.atr14 is not None
    assert result.position_label
    assert result.target1 == result.center20
