# -*- coding: utf-8 -*-
"""biz 包单元测试：CompiledStrategy / StrategyValidator / StrategyDecision。"""

import numpy as np
import pandas as pd
import pytest

from StockInvestmentTool.biz.models import StrategyContext
from StockInvestmentTool.biz.strategy import (
    CompiledStrategy,
    StrategySpec,
    compile_strategy,
    validate_strategy,
)


def make_ctx(close_last=11.8, position_state="none", quantity=0.0, cash=100000.0):
    close = np.array([10.0, 10.2, 10.5, 10.3, 10.6, 10.9, 11.0, 11.2, 11.5, close_last])
    df = pd.DataFrame({
        "date": pd.date_range("2026-08-01", periods=10, freq="B"),
        "code": "sh600908",
        "open": close * 0.99, "high": close * 1.02, "low": close * 0.98, "close": close,
        "volume": [1000] * 10, "amount": [10000] * 10,
    })
    return StrategyContext(
        symbol="sh600908",
        evaluation_time="2026-08-14T15:00:00Z",
        data_as_of="2026-08-14",
        market_data=df,
        position_state=position_state,
        position_quantity=quantity,
        position_state_avg_cost=10.0,
        cash_available=cash,
    )


def make_spec():
    return StrategySpec(
        strategy_id="trend_pullback",
        name="趋势回踩",
        version="1",
        entry_rules=[{
            "rule_id": "pullback_entry",
            "action": "BUY",
            "position_ratio": 0.2,
            "when": {"type": "comparison", "left": {"field": "close"},
                     "operator": ">", "right": {"value": 11}},
        }],
        exit_rules=[{
            "rule_id": "hard_stop_sell",
            "action": "SELL_ALL",
            "when": {"type": "comparison", "left": {"field": "close"},
                     "operator": "<", "right": {"value": 9}},
        }],
        risk={"hard_stop_ratio": 0.1, "max_position_ratio": 0.3},
        position_sizing={"mode": "fixed_ratio", "initial_ratio": 0.2},
        execution={"signal_at": "close", "execute_at": "next_open"},
        benchmark="sh000300",
    )


class TestValidate:
    def test_valid_spec(self):
        result = validate_strategy(make_spec())
        assert result["valid"], result["errors"]
        assert "close" in result["dependencies"]
        assert result["config_hash"]

    def test_missing_id(self):
        spec = make_spec()
        spec.strategy_id = ""
        result = validate_strategy(spec)
        assert not result["valid"]

    def test_no_rules(self):
        spec = make_spec()
        spec.entry_rules = []
        spec.exit_rules = []
        result = validate_strategy(spec)
        assert not result["valid"]

    def test_bad_action(self):
        spec = make_spec()
        spec.entry_rules[0]["action"] = "FROB"
        result = validate_strategy(spec)
        assert not result["valid"]

    def test_bad_ratio(self):
        spec = make_spec()
        spec.position_sizing["initial_ratio"] = 1.5
        result = validate_strategy(spec)
        assert not result["valid"]

    def test_compile_raises_on_invalid(self):
        spec = make_spec()
        spec.position_sizing["initial_ratio"] = 5
        with pytest.raises(ValueError):
            compile_strategy(spec)


class TestEvaluate:
    def test_buy_signal(self):
        strategy = compile_strategy(make_spec(), strategy_version_id="sv_test")
        ctx = make_ctx(close_last=11.8)
        dec = strategy.evaluate(ctx)
        assert dec.action == "BUY"
        assert dec.quantity_ratio == pytest.approx(0.2)
        assert dec.decision_trace["final_action"] == "BUY"
        assert len(dec.decision_trace["triggered_rules"]) >= 1
        assert dec.input_snapshot["symbol"] == "sh600908"

    def test_no_action_when_below(self):
        strategy = compile_strategy(make_spec(), strategy_version_id="sv_test")
        ctx = make_ctx(close_last=10.0)  # 不满足 > 11
        dec = strategy.evaluate(ctx)
        assert dec.action == "NO_ACTION"

    def test_hard_stop_sells_all(self):
        strategy = compile_strategy(make_spec(), strategy_version_id="sv_test")
        # 持有中，close 8.5 <= 成本 10 * (1-0.1)=9 → 硬止损
        ctx = make_ctx(close_last=8.5, position_state="holding", quantity=1000)
        dec = strategy.evaluate(ctx)
        assert dec.action == "SELL_ALL"
        assert "硬止损" in dec.reason

    def test_insufficient_cash_suppresses_buy(self):
        strategy = compile_strategy(make_spec(), strategy_version_id="sv_test")
        ctx = make_ctx(close_last=11.8, cash=0.0)
        dec = strategy.evaluate(ctx)
        assert dec.action == "NO_ACTION"
        assert "现金不足" in dec.reason

    def test_deterministic(self):
        strategy = compile_strategy(make_spec(), strategy_version_id="sv_test")
        ctx = make_ctx()
        d1 = strategy.evaluate(ctx)
        d2 = strategy.evaluate(ctx)
        assert d1.action == d2.action
        assert d1.decision_trace == d2.decision_trace

    def test_suppressed_rules_recorded(self):
        strategy = compile_strategy(make_spec(), strategy_version_id="sv_test")
        # 现金不足：buy 被抑制，出现在 suppressed 但 trace 有记录
        ctx = make_ctx(close_last=11.8, cash=0.0)
        dec = strategy.evaluate(ctx)
        assert any(r["passed"] for r in dec.decision_trace["triggered_rules"])
        assert dec.decision_trace["final_action"] == "NO_ACTION"

    def test_config_hash_stable(self):
        spec = make_spec()
        assert spec.config_hash() == spec.config_hash()
        spec2 = make_spec()
        assert spec.config_hash() == spec2.config_hash()

    def test_triggered_lower_priority_rule_is_suppressed(self):
        spec = make_spec()
        spec.entry_rules.append({
            "rule_id": "lower", "action": "BUY", "priority": 1,
            "when": {"type": "comparison", "left": {"field": "close"},
                     "operator": ">", "right": {"value": 11}},
        })
        spec.entry_rules[0]["priority"] = 100
        decision = compile_strategy(spec, strategy_version_id="sv_test").evaluate(make_ctx())
        suppressed = decision.decision_trace["suppressed_rules"]
        assert any(item.get("rule_id") == "lower" and item.get("suppressed_by") == "pullback_entry"
                   for item in suppressed)

    def test_sell_action_beats_higher_priority_buy(self):
        spec = make_spec()
        spec.entry_rules[0]["priority"] = 999
        spec.exit_rules[0]["priority"] = 1
        spec.exit_rules[0]["when"] = {"type": "comparison", "left": {"field": "close"},
                                       "operator": ">", "right": {"value": 5}}
        decision = compile_strategy(spec, strategy_version_id="sv_test").evaluate(make_ctx())
        assert decision.action == "SELL_ALL"
        assert decision.decision_trace["triggered_rules"][0]["rule_id"] == "hard_stop_sell"
