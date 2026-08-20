"""core — 核心抽象层：方案配置模型 + 注册中心 + 统一分析引擎"""

from StockInvestmentTool.core.scheme import (
    SchemeConfig,
    BuyRuleConfig,
    SellRuleConfig,
    RiskConfig,
    BacktestConfig,
    GridSearchConfig,
    load_scheme_from_dict,
    load_scheme_from_yaml,
)
from StockInvestmentTool.core.registry import (
    SchemeRegistry,
    SchemeNotFoundError,
    SchemeValidationError,
)

__all__ = [
    "SchemeConfig",
    "BuyRuleConfig",
    "SellRuleConfig",
    "RiskConfig",
    "BacktestConfig",
    "GridSearchConfig",
    "load_scheme_from_dict",
    "load_scheme_from_yaml",
    "SchemeRegistry",
    "SchemeNotFoundError",
    "SchemeValidationError",
]
