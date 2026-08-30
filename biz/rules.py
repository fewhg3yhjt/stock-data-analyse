# -*- coding: utf-8 -*-
"""RuleRegistry：ConditionSpec / RuleSpec 的统一执行器。

依据 docs/DOMAIN_MODEL_AND_CONTRACTS.md §4.2 与 docs/STRATEGY_CORE_AND_SIMULATION_DESIGN.md §3.2。
ConditionSpec 只是传给 RuleRegistry 的结构化配置；每个条件评估返回
passed / actual_values / threshold_values / explanation / dependencies / evaluation_status。

取值来源：行情字段（df 列）、指标（df 已计算列或 IndicatorRegistry.compute）、常量。
防未来数据：条件评估只使用 index <= at 的数据。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import numpy as np
import pandas as pd

from StockInvestmentTool.biz.models import (
    ConditionEvalResult,
    validate_condition_spec,
)

logger = logging.getLogger(__name__)

# comparison 支持的运算符
_COMPARISON_OPS = {">", ">=", "<", "<=", "==", "!="}


class ConditionScope:
    """条件评估上下文：包装 DataFrame + 指标提供者，解析 ref 为 Series。"""

    def __init__(
        self,
        df: pd.DataFrame,
        indicator_ctx: Any = None,
        registry: Any = None,
    ):
        self.df = df
        self.indicator_ctx = indicator_ctx
        self._registry = registry
        self._series_cache: dict[str, pd.Series] = {}

    # ── ref 解析 ──────────────────────────────────────────

    def _resolve_ref(self, ref: dict) -> pd.Series:
        """把 {field|indicator|value} ref 解析为 Series。

        value 常量 → 与 df 等长的常量 Series。
        field/indicator → 优先 df 列；否则用 registry.compute 现算。
        """
        if not isinstance(ref, dict):
            raise ValueError(f"condition ref must be dict, got {ref!r}")
        if "value" in ref:
            val = ref["value"]
            return pd.Series(float(val), index=self.df.index, dtype=float)

        name = ref.get("field") or ref.get("indicator")
        if not name:
            raise ValueError(f"ref must have field/indicator/value: {ref!r}")

        if name in self._series_cache:
            return self._series_cache[name]

        if name in self.df.columns:
            s = self.df[name]
            self._series_cache[name] = s
            return s

        # 指标现算
        if self._registry is not None:
            computed = self._registry.compute(self.df, [name])
            if name in computed:
                self._series_cache[name] = computed[name]
                return computed[name]
        if self.indicator_ctx is not None:
            try:
                s = self.indicator_ctx._compute_series(name)  # noqa: SLF001 复用指标序列
                self._series_cache[name] = s
                return s
            except Exception as e:  # noqa: BLE001
                logger.debug("indicator series unavailable %s: %s", name, e)

        raise ValueError(f"字段/指标不存在且无法计算: {name}")

    def series(self, ref: dict) -> pd.Series:
        return self._resolve_ref(ref)

    def value(self, ref: dict, at: int) -> float:
        s = self._resolve_ref(ref)
        if len(s) == 0:
            return float("nan")
        idx = min(max(at, 0), len(s) - 1)
        v = s.iloc[idx]
        if v is None or pd.isna(v):
            return float("nan")
        return float(v)

    def window(self, ref: dict, at: int, days: int) -> pd.Series:
        """取 [at-days+1, at] 窗口序列（防未来数据）。"""
        s = self._resolve_ref(ref)
        lo = max(0, at - days + 1)
        return s.iloc[lo : at + 1]


# ---------------------------------------------------------------------------
# 条件执行器
# ---------------------------------------------------------------------------

def _fmt(v: Any) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "n/a"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def _ev_at(scope: ConditionScope, at: int) -> ConditionEvalResult:
    return ConditionEvalResult(passed=True, actual_values={}, threshold_values={})


def _eval_comparison(scope: ConditionScope, spec: dict, at: int) -> ConditionEvalResult:
    op = spec.get("operator")
    if op not in _COMPARISON_OPS:
        return ConditionEvalResult(passed=False, explanation=f"不支持的比较运算符: {op}",
                                   evaluation_status="error")
    try:
        lv = scope.value(spec.get("left", {}), at)
        rv = scope.value(spec.get("right", {}), at)
    except ValueError as e:
        return ConditionEvalResult(passed=False, explanation=str(e), evaluation_status="error")
    if np.isnan(lv) or np.isnan(rv):
        return ConditionEvalResult(
            passed=False, actual_values={"left": lv, "right": rv},
            threshold_values={"right": rv},
            explanation="比较条件缺值（missing_data）",
            evaluation_status="missing_data",
        )
    passed = {
        ">": lv > rv, ">=": lv >= rv, "<": lv < rv, "<=": lv <= rv,
        "==": lv == rv, "!=": lv != rv,
    }[op]
    return ConditionEvalResult(
        passed=passed,
        actual_values={"left": lv},
        threshold_values={"right": rv},
        explanation=f"{_fmt(lv)} {op} {_fmt(rv)}",
        dependencies=[spec.get("left", {}).get("field") or spec.get("left", {}).get("indicator"),
                      spec.get("right", {}).get("field") or spec.get("right", {}).get("indicator")],
    )


def _eval_cross(scope: ConditionScope, spec: dict, at: int) -> ConditionEvalResult:
    direction = spec.get("direction")  # above / below
    if direction not in {"above", "below"}:
        return ConditionEvalResult(passed=False, explanation=f"cross direction 非法: {direction}",
                                   evaluation_status="error")
    if at < 1:
        return ConditionEvalResult(passed=False, explanation="无前一日数据，无法判定交叉",
                                   evaluation_status="missing_data")
    try:
        cur_l = scope.value(spec.get("left", {}), at)
        cur_r = scope.value(spec.get("right", {}), at)
        prev_l = scope.value(spec.get("left", {}), at - 1)
        prev_r = scope.value(spec.get("right", {}), at - 1)
    except ValueError as e:
        return ConditionEvalResult(passed=False, explanation=str(e), evaluation_status="error")
    if any(np.isnan(v) for v in (cur_l, cur_r, prev_l, prev_r)):
        return ConditionEvalResult(passed=False, explanation="交叉条件缺值",
                                   evaluation_status="missing_data")
    if direction == "above":
        passed = prev_l <= prev_r and cur_l > cur_r
        desc = "上穿"
    else:
        passed = prev_l >= prev_r and cur_l < cur_r
        desc = "下穿"
    return ConditionEvalResult(
        passed=passed,
        actual_values={"left": cur_l, "left_prev": prev_l, "right": cur_r, "right_prev": prev_r},
        threshold_values={"right": cur_r},
        explanation=f"{_fmt(prev_l)}=>{_fmt(cur_l)} {desc} {_fmt(cur_r)}",
        dependencies=[spec.get("left", {}).get("indicator"), spec.get("right", {}).get("indicator")],
    )


def _eval_between(scope: ConditionScope, spec: dict, at: int) -> ConditionEvalResult:
    try:
        v = scope.value(spec.get("field", {}), at) if isinstance(spec.get("field"), dict) \
            else scope.value({"field": spec["field"]}, at)
        low = float(spec.get("low"))
        high = float(spec.get("high"))
    except ValueError as e:
        return ConditionEvalResult(passed=False, explanation=str(e), evaluation_status="error")
    if np.isnan(v):
        return ConditionEvalResult(passed=False, explanation="between 条件缺值", evaluation_status="missing_data")
    passed = low <= v <= high
    return ConditionEvalResult(
        passed=passed,
        actual_values={"value": v},
        threshold_values={"low": low, "high": high},
        explanation=f"{_fmt(v)} ∈ [{_fmt(low)}, {_fmt(high)}]",
        dependencies=[spec.get("field")],
    )


def _eval_consecutive(scope: ConditionScope, spec: dict, at: int) -> ConditionEvalResult:
    days = spec.get("days")
    inner = spec.get("condition")
    if not isinstance(days, int) or days < 1 or not isinstance(inner, dict):
        return ConditionEvalResult(passed=False, explanation="consecutive 配置非法", evaluation_status="error")
    count = 0
    ok = True
    for i in range(at, max(-1, at - days), -1):
        if i < 0:
            ok = False
            break
        res = evaluate_condition(inner, scope, i)
        if not res.passed:
            ok = False
            break
        count += 1
    return ConditionEvalResult(
        passed=ok and count >= days,
        actual_values={"consecutive_days": count},
        threshold_values={"days": days},
        explanation=f"连续 {count}/{days} 日满足",
        evaluation_status="evaluated",
    )


def _eval_count(scope: ConditionScope, spec: dict, at: int) -> ConditionEvalResult:
    window = spec.get("window")
    gte = spec.get("gte")
    inner = spec.get("condition")
    if not isinstance(window, int) or window < 1 or not isinstance(inner, dict) or gte is None:
        return ConditionEvalResult(passed=False, explanation="count 配置非法", evaluation_status="error")
    hits = 0
    for i in range(max(0, at - window + 1), at + 1):
        res = evaluate_condition(inner, scope, i)
        if res.passed:
            hits += 1
    passed = hits >= gte
    return ConditionEvalResult(
        passed=passed,
        actual_values={"hits": hits},
        threshold_values={"window": window, "gte": gte},
        explanation=f"{window} 日内满足 {hits} 次（>= {gte}）",
    )


def _eval_and(scope: ConditionScope, spec: dict, at: int) -> ConditionEvalResult:
    children = spec.get("conditions", [])
    results = [evaluate_condition(c, scope, at) for c in children]
    all_passed = all(r.passed for r in results)
    explanations = "; ".join(r.explanation for r in results)
    return ConditionEvalResult(
        passed=all_passed,
        explanation=f"AND({explanations})",
        evaluation_status="error" if any(r.evaluation_status == "error" for r in results) else "evaluated",
    )


def _eval_or(scope: ConditionScope, spec: dict, at: int) -> ConditionEvalResult:
    children = spec.get("conditions", [])
    results = [evaluate_condition(c, scope, at) for c in children]
    any_passed = any(r.passed for r in results)
    explanations = "; ".join(r.explanation for r in results)
    return ConditionEvalResult(
        passed=any_passed,
        explanation=f"OR({explanations})",
        evaluation_status="evaluated",
    )


def _eval_not(scope: ConditionScope, spec: dict, at: int) -> ConditionEvalResult:
    inner = spec.get("condition")
    if not isinstance(inner, dict):
        return ConditionEvalResult(passed=False, explanation="not 配置非法", evaluation_status="error")
    res = evaluate_condition(inner, scope, at)
    return ConditionEvalResult(
        passed=not res.passed,
        explanation=f"NOT({res.explanation})",
        evaluation_status=res.evaluation_status,
    )


_EVALUATORS: dict[str, Callable[[ConditionScope, dict, int], ConditionEvalResult]] = {
    "comparison": _eval_comparison,
    "cross": _eval_cross,
    "between": _eval_between,
    "consecutive": _eval_consecutive,
    "count": _eval_count,
    "and": _eval_and,
    "or": _eval_or,
    "not": _eval_not,
}


def evaluate_condition(spec: dict, scope: ConditionScope, at: int = -1) -> ConditionEvalResult:
    """评估单个 ConditionSpec。at 为 df 中的目标行索引（默认末行）。"""
    errors = validate_condition_spec(spec)
    if errors:
        return ConditionEvalResult(passed=False, explanation="; ".join(errors), evaluation_status="error")
    if at < 0:
        at = max(len(scope.df) - 1, 0)
    evaluator = _EVALUATORS[spec["type"]]
    try:
        return evaluator(scope, spec, at)
    except Exception as e:  # noqa: BLE001
        logger.exception("condition evaluation failed: %s", spec)
        return ConditionEvalResult(passed=False, explanation=f"评估异常: {e}", evaluation_status="error")


# ---------------------------------------------------------------------------
# RuleSpec 评估
# ---------------------------------------------------------------------------

def evaluate_rule(rule_spec: dict, scope: ConditionScope, at: int = -1) -> bool:
    """评估 RuleSpec.when 条件树，返回是否触发。"""
    when = rule_spec.get("when", rule_spec.get("conditions"))
    if not isinstance(when, dict):
        raise ValueError(f"rule must have when: {rule_spec!r}")
    return evaluate_condition(when, scope, at).passed


def evaluate_conditions_tree(spec: dict, scope: ConditionScope, at: int = -1) -> dict:
    """评估整棵条件树，返回结构化结果（含逐条件解释），供候选/决策解释。"""
    result = evaluate_condition(spec, scope, at)
    return {
        "passed": result.passed,
        "explanation": result.explanation,
        "evaluation_status": result.evaluation_status,
        "actual_values": result.actual_values,
        "threshold_values": result.threshold_values,
    }


def collect_dependencies(spec: dict) -> list[str]:
    """递归收集条件树依赖的指标/字段名。"""
    deps: list[str] = []
    if not isinstance(spec, dict):
        return deps

    def walk(node: dict):
        for key in ("field", "indicator"):
            if key in node:
                name = node.get(key)
                if isinstance(name, str):
                    deps.append(name)
        for ctype in ("conditions", "condition"):
            child = node.get(ctype)
            if isinstance(child, list):
                for c in child:
                    if isinstance(c, dict):
                        walk(c)
            elif isinstance(child, dict):
                walk(child)
        for key in ("left", "right"):
            ref = node.get(key)
            if isinstance(ref, dict):
                for k in ("field", "indicator"):
                    if k in ref and isinstance(ref[k], str):
                        deps.append(ref[k])

    walk(spec)
    return sorted(set(deps))