# -*- coding: utf-8 -*-
"""CompiledStrategy / StrategyValidator / evaluate → StrategyDecision。

依据 docs/DOMAIN_MODEL_AND_CONTRACTS.md §4 与 docs/STRATEGY_CORE_AND_SIMULATION_DESIGN.md。
策略版本不可变；一次评估输出一个主动作 + 完整 decision_trace
（evaluated_rules / triggered_rules / suppressed_rules / final_action）。

策略执行不读取文件/数据库/网络，所有输入由调用方注入 StrategyContext。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from StockInvestmentTool.biz.models import (
    ACTIONS,
    StrategyContext,
    StrategyDecision,
    now_utc,
    new_id,
    stable_hash,
)

logger = logging.getLogger(__name__)

# 动作优先级（数字越小优先级越高）—— 风险/强制退出最高
ACTION_PRIORITY = {
    "SELL_ALL": 0,
    "SELL_PARTIAL": 1,
    "BUY_MORE": 2,
    "BUY": 3,
    "HOLD": 4,
    "WAIT": 5,
    "NO_ACTION": 6,
    "WATCH": 7,
}


@dataclass
class StrategySpec:
    """策略配置（SchemeConfig 载体，持久化前必过校验）。

    字段与 docs/DOMAIN_MODEL_AND_CONTRACTS.md §4.3 SchemeConfig 对齐。
    """
    strategy_id: str
    name: str = ""
    version: str = "1"
    entry_rules: list = field(default_factory=list)     # [RuleSpec]
    exit_rules: list = field(default_factory=list)      # [RuleSpec]
    risk: dict = field(default_factory=dict)            # hard_stop_ratio / max_position_ratio 等
    position_sizing: dict = field(default_factory=dict)  # mode / initial_ratio
    execution: dict = field(default_factory=dict)        # signal_at / execute_at
    benchmark: str = ""

    def config_hash(self) -> str:
        return stable_hash({
            "strategy_id": self.strategy_id,
            "name": self.name,
            "version": self.version,
            "entry_rules": self.entry_rules,
            "exit_rules": self.exit_rules,
            "risk": self.risk,
            "position_sizing": self.position_sizing,
            "execution": self.execution,
            "benchmark": self.benchmark,
        })


@dataclass
class CompiledStrategy:
    """SchemeConfig 解析后的内存运行对象，不持久化、不读取外部。"""

    spec: StrategySpec
    config_hash: str = ""
    strategy_version_id: str | None = None

    def __post_init__(self):
        if not self.config_hash:
            self.config_hash = self.spec.config_hash()

    # ── 评估入口 ──────────────────────────────────────────

    def evaluate(self, context: StrategyContext) -> StrategyDecision:
        if not self.strategy_version_id:
            raise ValueError("strategy_version_id is required for a formal strategy decision")
        evaluator = StrategyEvaluator(self)
        return evaluator.evaluate(context)


class StrategyEvaluator:
    """单次策略评估：评估规则 → 冲突仲裁 → 决策轨迹。"""

    def __init__(self, strategy: CompiledStrategy):
        self.strategy = strategy
        self.spec = strategy.spec

    def evaluate(self, context: StrategyContext) -> StrategyDecision:
        evaluated: list[dict] = []
        triggered: list[dict] = []
        suppressed: list[dict] = []

        # 1. 先评估卖出/风控（优先级高）
        for rule in self.spec.exit_rules:
            entry = self._eval_rule(rule, context)
            evaluated.append(entry)
            if entry["passed"]:
                triggered.append(entry)
            else:
                suppressed.append(entry)

        # 2. 评估买入
        for rule in self.spec.entry_rules:
            entry = self._eval_rule(rule, context)
            evaluated.append(entry)
            if entry["passed"]:
                triggered.append(entry)
            else:
                suppressed.append(entry)

        # 3. 风险强制退出（硬止损优先于所有）
        risk_action = self._eval_risk(context)

        final_action, reason, used_trigger = self._arbitrate(
            triggered, risk_action, context
        )

        decision_trace = {
            "evaluated_rules": evaluated,
            "triggered_rules": triggered,
            "suppressed_rules": suppressed,
            "final_action": final_action,
        }
        return StrategyDecision(
            decision_id=new_id("dec"),
            strategy_id=self.spec.strategy_id,
            strategy_version=self.spec.version,
            strategy_version_id=self.strategy.strategy_version_id,
            symbol=context.symbol,
            decision_time=context.evaluation_time,
            data_as_of=context.data_as_of,
            action=final_action,
            quantity_ratio=self._quantity_ratio(final_action, context),
            price=self._reference_price(context),
            input_dependencies=[r.get("rule_id") for r in evaluated if r.get("rule_id")],
            input_snapshot=self._snapshot(context),
            decision_trace=decision_trace,
            reason=reason,
            valid_until=self._valid_until(final_action),
        )

    # ── 内部 ──────────────────────────────────────────────

    def _eval_rule(self, rule: dict, context: StrategyContext) -> dict:
        """评估一条 RuleSpec。rule = {rule_id, action, when, position_ratio, ...}。"""
        from StockInvestmentTool.biz.rules import ConditionScope, evaluate_condition

        scope = ConditionScope(context.market_data, indicator_ctx=context.indicator_context)
        at = -1
        res = evaluate_condition(rule.get("when", {}), scope, at)
        return {
            "rule_id": rule.get("rule_id", ""),
            "action": rule.get("action", "NO_ACTION"),
            "passed": res.passed,
            "explanation": res.explanation,
            "evaluation_status": res.evaluation_status,
            "position_ratio": rule.get("position_ratio"),
            "priority": int(rule.get("priority", 0)),
        }

    def _eval_risk(self, context: StrategyContext) -> dict | None:
        """评估风控，返回强制动作（dict 含 action/reason）或 None。"""
        hard_stop = self.spec.risk.get("hard_stop_ratio")
        if hard_stop and context.position_state in {"open", "holding", "accumulating"}:
            avg_cost = context.position_state_avg_cost
            price = self._reference_price(context)
            if avg_cost and price and price <= avg_cost * (1 - float(hard_stop)):
                return {"action": "SELL_ALL", "reason": f"硬止损触发（{price:.4g} <= 成本*{1 - float(hard_stop):.4g}）"}
        return None

    def _arbitrate(self, triggered: list[dict], risk_action: dict | None,
                   context: StrategyContext) -> tuple[str, str, dict | None]:
        """冲突仲裁：风险/强制退出 > 全卖 > 部分卖 > 加仓 > 首买 > 持有/等待。"""
        if risk_action:
            return risk_action["action"], risk_action["reason"], None

        if not triggered:
            return "NO_ACTION", "无规则触发", None

        best = min(
            enumerate(triggered),
            key=lambda item: (-int(item[1].get("priority", 0)),
                              ACTION_PRIORITY.get(item[1]["action"], 99), item[0]),
        )[1]
        action = best["action"]

        # 首次买入/加仓需现金；无仓位时 BUY，有仓位时 BUY_MORE 需有仓位
        if action == "BUY" and context.position_quantity > 0:
            return "BUY_MORE", best["explanation"], best
        if action == "BUY" and context.cash_available <= 0:
            return "NO_ACTION", "现金不足，买入被抑制", best
        if action == "BUY_MORE" and context.position_quantity <= 0:
            return "BUY", best["explanation"], best

        return action, best["explanation"], best

    def _quantity_ratio(self, action: str, context: StrategyContext) -> float | None:
        if action in {"BUY", "BUY_MORE"}:
            ps = self.spec.position_sizing
            return float(ps.get("initial_ratio", 0.2) if ps else 0.2)
        if action in {"SELL_ALL"}:
            return 1.0
        if action in {"SELL_PARTIAL"}:
            return float(self.spec.risk.get("partial_sell_ratio", 0.5))
        return None

    def _reference_price(self, context: StrategyContext) -> float | None:
        if context.market_data is not None and len(context.market_data):
            try:
                import pandas as pd
                close = context.market_data["close"]
                if hasattr(close, "iloc") and len(close):
                    v = close.iloc[-1]
                    return float(v) if v is not None and not pd.isna(v) else None
            except Exception:  # noqa: BLE001
                return None
        return None

    def _snapshot(self, context: StrategyContext) -> dict:
        """输入快照：保存当时实际使用的指标/上下文值。"""
        snap: dict = {
            "symbol": context.symbol,
            "data_as_of": context.data_as_of,
            "position_state": context.position_state,
            "position_quantity": context.position_quantity,
            "cash_available": context.cash_available,
            "market_regime": context.market_regime,
        }
        if context.indicator_context is not None:
            try:
                latest = context.indicator_context.latest()
                if isinstance(latest, dict):
                    snap["indicator_values"] = {k: v for k, v in latest.items()
                                                if isinstance(v, (int, float))}
            except Exception as e:  # noqa: BLE001
                logger.debug("input snapshot indicator values unavailable: %s", e)
        return snap

    def _valid_until(self, action: str) -> str | None:
        # 买入信号默认 3 个交易日有效
        if action in {"BUY", "BUY_MORE", "SELL_ALL", "SELL_PARTIAL"}:
            return ""
        return None


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def validate_strategy(spec: StrategySpec) -> dict:
    """校验策略，返回 {valid, errors, warnings, dependencies, config_hash}。"""
    errors: list[str] = []
    warnings: list[str] = []

    if not spec.strategy_id:
        errors.append("strategy_id 不能为空")
    if not spec.version:
        errors.append("version 不能为空")

    if not spec.entry_rules and not spec.exit_rules:
        errors.append("入场和出场规则至少各一条，除非声明为纯选股策略")

    from StockInvestmentTool.biz.rules import collect_dependencies

    deps: list[str] = []
    for kind, rules in (("entry", spec.entry_rules), ("exit", spec.exit_rules)):
        for rule in rules:
            if not isinstance(rule, dict):
                errors.append(f"{kind} rule 必须是 dict")
                continue
            rule_id = rule.get("rule_id", "")
            action = rule.get("action")
            if action not in ACTIONS:
                errors.append(f"rule {rule_id or '?'}: 非法 action {action!r}")
            when = rule.get("when")
            if not isinstance(when, dict):
                errors.append(f"rule {rule_id or '?'}: 缺少 when 条件树")
            else:
                from StockInvestmentTool.biz.models import validate_condition_spec
                errors.extend(f"rule {rule_id}: {e}" for e in validate_condition_spec(when))
                deps.extend(collect_dependencies(when))

    # 仓位比例校验
    ps = spec.position_sizing or {}
    ratio = ps.get("initial_ratio", 1.0)
    try:
        if not (0 < float(ratio) <= 1):
            errors.append("position_sizing.initial_ratio 必须在 (0, 1]")
    except (TypeError, ValueError):
        errors.append("position_sizing.initial_ratio 非法")

    # 硬止损参数合理
    risk = spec.risk or {}
    if "hard_stop_ratio" in risk:
        try:
            hs = float(risk["hard_stop_ratio"])
            if not (0 < hs < 1):
                errors.append("risk.hard_stop_ratio 必须在 (0, 1)")
        except (TypeError, ValueError):
            errors.append("risk.hard_stop_ratio 非法")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "dependencies": sorted(set(deps)),
        "config_hash": spec.config_hash(),
    }


def compile_strategy(spec: StrategySpec, strategy_version_id: str | None = None) -> CompiledStrategy:
    """校验并编译策略。校验失败抛 ValueError。"""
    result = validate_strategy(spec)
    if not result["valid"]:
        raise ValueError(f"策略校验失败: {'; '.join(result['errors'])}")
    return CompiledStrategy(spec=spec, config_hash=result["config_hash"],
                            strategy_version_id=strategy_version_id)
