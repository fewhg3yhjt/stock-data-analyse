"""Feature-condition rule engine.

This complements the existing strategy RuleRegistry.  It evaluates condition
trees against already-computed feature values and never performs business
actions by itself.
"""

from __future__ import annotations

from StockInvestmentTool.repositories.backend_domain import BackendDomainRepository
from StockInvestmentTool.services.feature_service import FeatureService


LOGICAL_OPERATORS = {"AND", "OR"}
CONDITION_OPERATORS = {
    "EQ",
    "NE",
    "GT",
    "GTE",
    "LT",
    "LTE",
    "BETWEEN",
    "IS_TRUE",
    "IS_FALSE",
    "IN",
}


class RuleEngine:
    def __init__(self, repository: BackendDomainRepository | None = None):
        self.repository = repository or BackendDomainRepository()
        self.features = FeatureService(self.repository)

    def validate_rule(self, expression: dict, *, usage: str, context: str) -> None:
        self._validate_node(expression, usage=usage, context=context)

    def evaluate_rule(self, expression: dict, feature_values: dict) -> bool:
        return bool(self._evaluate_node(expression, feature_values))

    def _validate_node(self, node: dict, *, usage: str, context: str) -> None:
        if not isinstance(node, dict):
            raise ValueError("rule node must be an object")
        operator = node.get("operator")
        if operator in LOGICAL_OPERATORS:
            children = node.get("children") or []
            if not children:
                raise ValueError(f"{operator} rule requires children")
            for child in children:
                self._validate_node(child, usage=usage, context=context)
            return
        if operator not in CONDITION_OPERATORS:
            raise ValueError(f"unsupported rule operator: {operator}")
        feature_id = node.get("feature")
        if not feature_id:
            raise ValueError("condition node requires feature")
        self.features.require_feature_usage(feature_id, usage, context)
        if operator == "BETWEEN":
            value = node.get("value")
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError("BETWEEN requires value [low, high]")
        if operator == "IN" and not isinstance(node.get("value"), list):
            raise ValueError("IN requires list value")

    def _evaluate_node(self, node: dict, feature_values: dict) -> bool:
        operator = node.get("operator")
        if operator == "AND":
            return all(self._evaluate_node(child, feature_values) for child in node.get("children") or [])
        if operator == "OR":
            return any(self._evaluate_node(child, feature_values) for child in node.get("children") or [])
        feature_id = node.get("feature")
        actual = feature_values.get(feature_id)
        expected = node.get("value")
        if operator == "EQ":
            return actual == expected
        if operator == "NE":
            return actual != expected
        if operator == "GT":
            return actual is not None and actual > expected
        if operator == "GTE":
            return actual is not None and actual >= expected
        if operator == "LT":
            return actual is not None and actual < expected
        if operator == "LTE":
            return actual is not None and actual <= expected
        if operator == "BETWEEN":
            low, high = expected
            return actual is not None and low <= actual <= high
        if operator == "IS_TRUE":
            return actual is True
        if operator == "IS_FALSE":
            return actual is False
        if operator == "IN":
            return actual in expected
        raise ValueError(f"unsupported rule operator: {operator}")
