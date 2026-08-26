"""V6 backtest state transitions use the shared state machine."""

from __future__ import annotations

from StockInvestmentTool.backtest.engine_v6 import BacktestEngineV6, _Position
from StockInvestmentTool.strategy.position_state import (
    STATE_ACCUMULATING, STATE_HOLDING, STATE_LEFT_SIDE, STATE_RIGHT_SIDE,
    STATE_CLOSED,
)


def test_v6_position_state_initializes_closed(kline):
    engine = BacktestEngineV6(kline)
    pos = _Position()

    assert pos.state == STATE_CLOSED
    assert engine.state_machine.transition(STATE_ACCUMULATING, "bought") == STATE_HOLDING


def test_v6_state_machine_has_expected_sell_path():
    from StockInvestmentTool.strategy.position_state import PositionStateMachine

    sm = PositionStateMachine()
    assert sm.transition(STATE_HOLDING, "left_tp") == STATE_LEFT_SIDE
    assert sm.transition(STATE_LEFT_SIDE, "breakout") == STATE_RIGHT_SIDE
    assert sm.transition(STATE_RIGHT_SIDE, "closed") == STATE_CLOSED


def test_v6_reads_registered_rule_parameters(kline):
    from StockInvestmentTool.core.scheme import SchemeConfig, SellRuleConfig

    scheme = SchemeConfig(name="v6-test", sell_rules=[
        SellRuleConfig("left_side_fixed", {"ratio_by_type": {"B": 0.25}}),
        SellRuleConfig("right_side_trailing", {"drawdown_by_type": {"B": 0.08}}),
    ])
    engine = BacktestEngineV6(kline, scheme=scheme)

    assert engine.left_ratio_by_type["B"] == 0.25
    assert engine.right_dd_by_type["B"] == 0.08
