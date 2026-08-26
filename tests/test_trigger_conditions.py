"""通知触发器条件执行测试。"""

from __future__ import annotations

from StockInvestmentTool.web.scheduler import _evaluate_trigger_condition, _orders_lines


def _data(advice_type="partial_sell"):
    return {
        "summary": {"position_count": 1, "total_pnl_pct": 2.5},
        "positions": [{
            "stock_name": "测试股", "stock_code": "sh600900",
            "current_price": 10, "unrealized_pnl_pct": 2.5,
            "advice": {"advice_type": advice_type, "reason": "测试"},
            "advice_label": "减仓",
        }],
    }


def test_action_condition_filters_advice_types():
    assert _evaluate_trigger_condition({"type": "action", "params": {"advice_types": ["partial_sell"]}}, _data())
    assert not _evaluate_trigger_condition({"type": "action", "params": {"advice_types": ["sell_all"]}}, _data())
    assert _orders_lines(_data(), {"sell_all"}) == []


def test_or_and_condition_result_is_individual():
    assert _evaluate_trigger_condition({"type": "action", "params": {"advice_types": ["partial_sell"]}}, _data())
    assert not _evaluate_trigger_condition({"type": "action", "params": {"advice_types": ["hold"]}}, _data())
