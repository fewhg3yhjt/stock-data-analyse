"""Feature metadata service."""

from __future__ import annotations

from StockInvestmentTool.domain.features import FeatureDefinition
from StockInvestmentTool.repositories.backend_domain import BackendDomainRepository


class FeatureService:
    def __init__(self, repository: BackendDomainRepository | None = None):
        self.repository = repository or BackendDomainRepository()

    def register_feature(self, feature: FeatureDefinition) -> FeatureDefinition:
        return self.repository.save_feature(feature)

    def get(self, feature_id: str) -> FeatureDefinition | None:
        return self.repository.get_feature(feature_id)

    def require_feature_usage(self, feature_id: str, usage: str, context: str) -> FeatureDefinition:
        feature = self.repository.get_feature(feature_id)
        if not feature:
            raise KeyError(f"unknown feature: {feature_id}")
        if not feature.supports(usage, context):
            raise ValueError(f"feature {feature_id} does not support {usage} in {context}")
        return feature
