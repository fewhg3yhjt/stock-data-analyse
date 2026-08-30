"""Rule definition domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field

from StockInvestmentTool.domain.features import now_text


RULE_DRAFT = "DRAFT"
RULE_ACTIVE = "ACTIVE"
RULE_ARCHIVED = "ARCHIVED"

VALID_RULE_STATUSES = {RULE_DRAFT, RULE_ACTIVE, RULE_ARCHIVED}


@dataclass
class RuleDefinition:
    rule_id: str
    name: str
    expression_json: dict
    status: str = RULE_DRAFT
    version: int = 1
    created_at: str = field(default_factory=now_text)
    updated_at: str = field(default_factory=now_text)

    def validate(self) -> None:
        if not self.rule_id:
            raise ValueError("rule_id is required")
        if not self.name:
            raise ValueError("rule name is required")
        if self.status not in VALID_RULE_STATUSES:
            raise ValueError(f"invalid rule status: {self.status}")
        if self.version < 1:
            raise ValueError("rule version must be >= 1")
        if not isinstance(self.expression_json, dict):
            raise ValueError("expression_json must be an object")
