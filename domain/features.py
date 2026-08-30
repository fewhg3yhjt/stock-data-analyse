"""Feature metadata domain objects.

Features are the backend representation of user-facing indicators.  Metadata
controls where a feature can be evaluated and which business usages may refer
to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


CATEGORY_TECHNICAL = "TECHNICAL"
CATEGORY_MARKET = "MARKET"
CATEGORY_INDUSTRY = "INDUSTRY"
CATEGORY_POSITION = "POSITION"
CATEGORY_FUNDAMENTAL = "FUNDAMENTAL"

VALUE_NUMBER = "NUMBER"
VALUE_BOOLEAN = "BOOLEAN"
VALUE_STRING = "STRING"

CONTEXT_SECURITY = "SECURITY"
CONTEXT_INDUSTRY = "INDUSTRY"
CONTEXT_MARKET = "MARKET"
CONTEXT_POSITION = "POSITION"

USAGE_SCREEN = "SCREEN"
USAGE_BUY = "BUY"
USAGE_SELL = "SELL"
USAGE_NOTIFY = "NOTIFY"

FREQUENCY_DAILY = "DAILY"
FREQUENCY_REALTIME = "REALTIME"

VALID_CATEGORIES = {
    CATEGORY_TECHNICAL,
    CATEGORY_MARKET,
    CATEGORY_INDUSTRY,
    CATEGORY_POSITION,
    CATEGORY_FUNDAMENTAL,
}
VALID_VALUE_TYPES = {VALUE_NUMBER, VALUE_BOOLEAN, VALUE_STRING}
VALID_CONTEXTS = {CONTEXT_SECURITY, CONTEXT_INDUSTRY, CONTEXT_MARKET, CONTEXT_POSITION}
VALID_USAGES = {USAGE_SCREEN, USAGE_BUY, USAGE_SELL, USAGE_NOTIFY}
VALID_FREQUENCIES = {FREQUENCY_DAILY, FREQUENCY_REALTIME}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class FeatureDefinition:
    feature_id: str
    name: str
    category: str
    value_type: str
    required_context: str
    supported_usages: list[str]
    source_datasets: list[str] = field(default_factory=list)
    calculation_key: str = ""
    lookback_days: int = 0
    frequency: str = FREQUENCY_DAILY
    version: int = 1
    enabled: bool = True
    created_at: str = field(default_factory=now_text)
    updated_at: str = field(default_factory=now_text)

    def validate(self) -> None:
        if not self.feature_id:
            raise ValueError("feature_id is required")
        if not self.name:
            raise ValueError("feature name is required")
        if self.category not in VALID_CATEGORIES:
            raise ValueError(f"invalid feature category: {self.category}")
        if self.value_type not in VALID_VALUE_TYPES:
            raise ValueError(f"invalid feature value_type: {self.value_type}")
        if self.required_context not in VALID_CONTEXTS:
            raise ValueError(f"invalid feature required_context: {self.required_context}")
        if self.frequency not in VALID_FREQUENCIES:
            raise ValueError(f"invalid feature frequency: {self.frequency}")
        if not self.supported_usages:
            raise ValueError("supported_usages is required")
        invalid = [u for u in self.supported_usages if u not in VALID_USAGES]
        if invalid:
            raise ValueError(f"invalid feature usage: {invalid[0]}")
        if self.lookback_days < 0:
            raise ValueError("lookback_days must be >= 0")
        if self.version < 1:
            raise ValueError("version must be >= 1")

    def supports(self, usage: str, context: str) -> bool:
        return self.enabled and usage in self.supported_usages and context == self.required_context
