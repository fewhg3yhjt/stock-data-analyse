# -*- coding: utf-8 -*-
"""biz 包单元测试：ConditionSpec/RuleSpec 执行器。"""

import numpy as np
import pandas as pd
import pytest

from StockInvestmentTool.biz.rules import (
    ConditionScope,
    collect_dependencies,
    evaluate_condition,
    evaluate_rule,
    evaluate_conditions_tree,
)


def make_df():
    """构造 10 日行情，close 呈上升趋势，含 ma5/ma20 指标列。"""
    dates = pd.date_range("2026-08-01", periods=10, freq="B")
    close = np.array([10.0, 10.2, 10.5, 10.3, 10.6, 10.9, 11.0, 11.2, 11.5, 11.8])
    open_ = close * 0.99
    high = close * 1.02
    low = close * 0.98
    df = pd.DataFrame({
        "date": dates,
        "code": "sh600908",
        "open": open_, "high": high, "low": low, "close": close,
        "volume": [1000] * 10, "amount": [10000] * 10,
        "ma5": pd.Series(close).rolling(5, min_periods=1).mean(),
        "ma20": pd.Series(close).rolling(20, min_periods=1).mean(),
    })
    return df


@pytest.fixture
def scope():
    return ConditionScope(make_df())


class TestComparison:
    def test_gt_passes(self, scope):
        spec = {"type": "comparison", "left": {"field": "close"}, "operator": ">",
                "right": {"value": 11.0}}
        res = evaluate_condition(spec, scope, at=-1)
        assert res.passed
        assert res.actual_values["left"] == pytest.approx(11.8)

    def test_gt_fails(self, scope):
        spec = {"type": "comparison", "left": {"field": "close"}, "operator": "<",
                "right": {"value": 11.0}}
        res = evaluate_condition(spec, scope, at=-1)
        assert not res.passed

    def test_indicator_ref(self, scope):
        spec = {"type": "comparison", "left": {"field": "close"}, "operator": ">",
                "right": {"indicator": "ma5"}}
        res = evaluate_condition(spec, scope, at=-1)
        assert res.passed  # 11.8 > 均线

    def test_missing_data(self, scope):
        df = make_df()
        df["close"] = [np.nan] * 10
        s = ConditionScope(df)
        spec = {"type": "comparison", "left": {"field": "close"}, "operator": ">",
                "right": {"value": 5}}
        res = evaluate_condition(spec, s, at=-1)
        assert not res.passed
        assert res.evaluation_status == "missing_data"


class TestCross:
    def test_above_passes(self):
        df = make_df()
        # ma5 从 ma20 下方穿越到上方：前 4 日 10 持平，随后跳升至 20
        close = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 20.0, 20.0, 20.0, 20.0, 20.0])
        df["ma5"] = pd.Series(close).rolling(5, min_periods=1).mean()
        df["ma20"] = pd.Series(close).rolling(20, min_periods=1).mean()
        s = ConditionScope(df)
        spec = {"type": "cross", "left": {"indicator": "ma5"}, "direction": "above",
                "right": {"indicator": "ma20"}}
        res = evaluate_condition(spec, s, at=5)
        # index4: ma5=ma20=10（相等）; index5: ma5=12 > ma20=11.67 → 上穿
        assert res.passed


class TestBetween:
    def test_between(self, scope):
        spec = {"type": "between", "field": "close", "low": 11.0, "high": 12.0}
        res = evaluate_condition(spec, scope, at=-1)
        assert res.passed


class TestConsecutive:
    def test_consecutive(self, scope):
        # close 全部 > 10，连续 10 日成立
        spec = {"type": "consecutive", "condition": {"type": "comparison",
                "left": {"field": "close"}, "operator": ">", "right": {"value": 10}}, "days": 3}
        res = evaluate_condition(spec, scope, at=-1)
        assert res.passed

    def test_consecutive_fails(self, scope):
        spec = {"type": "consecutive", "condition": {"type": "comparison",
                "left": {"field": "close"}, "operator": "<", "right": {"value": 9}}, "days": 2}
        res = evaluate_condition(spec, scope, at=-1)
        assert not res.passed


class TestCount:
    def test_count(self, scope):
        # 10 日内 close>11 的日数：11.0/11.2/11.5/11.8 = 4 天
        spec = {"type": "count", "condition": {"type": "comparison",
                "left": {"field": "close"}, "operator": ">", "right": {"value": 11}}, "window": 10, "gte": 3}
        res = evaluate_condition(spec, scope, at=-1)
        assert res.passed


class TestAndOrNot:
    def test_and(self, scope):
        spec = {"type": "and", "conditions": [
            {"type": "comparison", "left": {"field": "close"}, "operator": ">", "right": {"value": 11}},
            {"type": "comparison", "left": {"field": "close"}, "operator": ">", "right": {"value": 5}},
        ]}
        assert evaluate_condition(spec, scope, at=-1).passed

    def test_or(self, scope):
        spec = {"type": "or", "conditions": [
            {"type": "comparison", "left": {"field": "close"}, "operator": "<", "right": {"value": 1}},
            {"type": "comparison", "left": {"field": "close"}, "operator": ">", "right": {"value": 5}},
        ]}
        assert evaluate_condition(spec, scope, at=-1).passed

    def test_not(self, scope):
        spec = {"type": "not", "condition": {"type": "comparison",
                "left": {"field": "close"}, "operator": "<", "right": {"value": 1}}}
        assert evaluate_condition(spec, scope, at=-1).passed


class TestValidation:
    def test_unknown_type(self, scope):
        res = evaluate_condition({"type": "foo"}, scope)
        assert not res.passed
        assert res.evaluation_status == "error"

    def test_missing_fields(self, scope):
        res = evaluate_condition({"type": "comparison"}, scope)
        assert res.evaluation_status == "error"


class TestRuleAndDeps:
    def test_rule_trigger(self, scope):
        rule = {"rule_id": "r1", "action": "BUY",
                "when": {"type": "comparison", "left": {"field": "close"},
                         "operator": ">", "right": {"value": 11}}}
        assert evaluate_rule(rule, scope, at=-1)

    def test_collect_dependencies(self):
        spec = {"type": "and", "conditions": [
            {"type": "comparison", "left": {"field": "close"}, "operator": ">",
             "right": {"indicator": "ma60"}},
            {"type": "cross", "left": {"indicator": "ma20"}, "direction": "above",
             "right": {"indicator": "ma60"}},
        ]}
        deps = collect_dependencies(spec)
        assert set(deps) == {"close", "ma20", "ma60"}

    def test_conditions_tree(self, scope):
        spec = {"type": "comparison", "left": {"field": "close"}, "operator": ">",
                "right": {"value": 11}}
        tree = evaluate_conditions_tree(spec, scope, at=-1)
        assert tree["passed"]
        assert "explanation" in tree