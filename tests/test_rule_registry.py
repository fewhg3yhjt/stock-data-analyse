"""FR-1.1 统一规则派发 测试。"""

from __future__ import annotations

import pytest

from StockInvestmentTool.strategy.context import RuleContext, RuleResult
from StockInvestmentTool.strategy.rule_registry import make_rule_registry


@pytest.fixture
def reg():
    return make_rule_registry()


def test_all_required_types_registered(reg):
    for kind, types in [
        ("buy", ["support_level", "trend_following", "market_state_arbiter"]),
        ("sell", ["hard_stop", "technical_stop", "left_side_fixed",
                  "right_side_trailing", "logic_stop", "price_stop", "time_stop"]),
    ]:
        for t in types:
            assert reg.has(kind, t), f"{kind}/{t} 未注册"


def test_types_enumerable(reg):
    assert "support_level" in reg.types("buy")
    assert "hard_stop" in reg.types("sell")


def test_hard_stop_rule_keeps_name_and_exposes_two_modes(reg):
    executor = reg.get("sell", "hard_stop")
    assert executor.description == "硬止损"
    schema = reg.schema("sell", "hard_stop")
    mode = next(field for field in schema if field["key"] == "mode")
    assert mode["options"] == ["fixed", "breakeven"]
    fixed = next(field for field in schema if field["key"] == "stop_loss_by_type")
    assert "A-E 是证券类型" in fixed["help"]
    assert "止损价" in fixed["help"]


def test_schema_present_for_all(reg):
    for kind in ("buy", "sell"):
        for t in reg.types(kind):
            schema = reg.schema(kind, t)
            assert schema, f"{kind}/{t} 缺 schema"
            assert all("key" in f and "label" in f and "type" in f for f in schema)


def test_dispatch_support_level(kline):
    from StockInvestmentTool.indicators.context import IndicatorContext
    ctx = RuleContext(
        df=kline,
        row=kline.iloc[-1],
        indicators=IndicatorContext(kline),
        current_price=float(kline["close"].iloc[-1]),
    )
    reg = make_rule_registry()
    res = reg.dispatch("buy", "support_level", ctx,
                       {"support_sources": ["MA60", "MIN(MA20,MA240)"],
                        "buy_stages": [{"label": "弱支撑", "position_index": 1, "ratio": 0.3}]})
    assert isinstance(res, RuleResult)
    assert "plan" in res.detail


def test_dispatch_unregistered_raises(reg):
    with pytest.raises(KeyError):
        reg.get("buy", "not_a_real_type")


def test_hard_stop_supports_fixed_and_breakeven_modes(reg):
    from StockInvestmentTool.strategy.context import RuleContext

    fixed = reg.dispatch("sell", "hard_stop", RuleContext(
        avg_cost=100, current_price=84, peak_price=110,
        row=__import__("pandas").Series({"low": 84}),
        extra={"stock_type": "B"},
    ), {"mode": "fixed", "stop_loss_by_type": {"B": 0.15}})
    assert fixed.triggered is True
    assert fixed.detail["stop_price"] == 85

    breakeven = reg.dispatch("sell", "hard_stop", RuleContext(
        avg_cost=100, current_price=92, peak_price=1000,
        row=__import__("pandas").Series({"low": 92}),
        extra={"stock_type": "B"},
    ), {"mode": "breakeven", "stop_loss_by_type": {"B": 0.15},
        "breakeven_activation_by_type": {"B": 0.08}})
    assert breakeven.triggered is True
    assert breakeven.detail["stop_price"] == 92.59
    assert breakeven.detail["activated"] is True


def test_right_side_trailing_requires_configured_profit_before_trigger(reg):
    from StockInvestmentTool.strategy.context import RuleContext

    result = reg.dispatch("sell", "right_side_trailing", RuleContext(
        avg_cost=100, current_price=108, peak_price=120,
        extra={"stock_type": "B"},
    ), {"drawdown_by_type": {"B": 0.05},
        "profit_activation_enabled": True,
        "profit_activation_basis": "current_price",
        "min_profit_for_activation": 0.10})
    assert result.triggered is False
    assert result.detail["profit_ready"] is False

    result = reg.dispatch("sell", "right_side_trailing", RuleContext(
        avg_cost=100, current_price=114, peak_price=120,
        extra={"stock_type": "B"},
    ), {"drawdown_by_type": {"B": 0.05},
        "profit_activation_enabled": True,
        "profit_activation_basis": "peak_price",
        "min_profit_for_activation": 0.10})
    assert result.triggered is True
    assert result.detail["profit_ready"] is True


def test_scheme_unified_rule_lookup():
    from StockInvestmentTool.core.scheme import load_scheme_from_dict
    scheme = load_scheme_from_dict({"name": "x", "buy_rules": [{"type": "support_level", "params": {}}]})
    assert scheme.rule("buy", "support_level").type == "support_level"


def test_dispatch_market_state_arbiter(kline):
    from StockInvestmentTool.indicators.context import IndicatorContext
    ctx = RuleContext(
        df=kline, row=kline.iloc[-1], indicators=IndicatorContext(kline),
        current_price=float(kline["close"].iloc[-1]),
        market_state="震荡市",
    )
    reg = make_rule_registry()
    res = reg.dispatch("buy", "market_state_arbiter", ctx, {})
    assert isinstance(res, RuleResult)
    assert res.action in ("buy_more", "hold")
