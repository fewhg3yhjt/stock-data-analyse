"""V6.0 规则 type → 执行类 注册中心（FR-1.1 统一派发）

本模块已并入统一 `RuleRegistry`（见 `strategy/rule_registry.py`），
保留旧函数作为**向后兼容适配层**，供存量调用方（analysis/backtest/单股对比、
prompt、screener 等）继续使用 `get_buy_executor`/`get_sell_executor`。

新增可执行规则 type：
    从 StockInvestmentTool.strategy.rule_registry import get_rule_registry
    reg = get_rule_registry()          # 全部 v4.5 + V6.0 type
    reg.dispatch("buy", "market_state_arbiter", ctx, params)
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from StockInvestmentTool.strategy.rule_registry import get_rule_registry

logger = logging.getLogger(__name__)


def is_v6_buy_type(rule_type: str) -> bool:
    """V6.0 专属买入 type 判断（V6.0 新增，v4.5 无）。"""
    return rule_type == "market_state_arbiter"


def is_v6_sell_type(rule_type: str) -> bool:
    """V6.0 专属卖出 type 判断（V6.0 新增，v4.5 无）。"""
    return rule_type in ("logic_stop", "price_stop", "time_stop")


def get_buy_executor(rule_type: str) -> Optional[Callable]:
    """按 type 取买入执行函数（经统一注册表适配）。

    返回的是注册表中的 executor（fn(ctx, params) -> RuleResult）包装：
      - 若同时注册了旧版可调用对象，此处返回该对象，保持旧签名兼容；
      - 返回 None 表示未注册。
    """
    reg = get_rule_registry()
    try:
        return reg.get("buy", rule_type)
    except KeyError:
        logger.debug("未注册买入规则 type: %s", rule_type)
        return None


def get_sell_executor(rule_type: str) -> Optional[Callable]:
    """按 type 取卖出执行函数（经统一注册表适配）。"""
    reg = get_rule_registry()
    try:
        return reg.get("sell", rule_type)
    except KeyError:
        logger.debug("未注册卖出规则 type: %s", rule_type)
        return None


def registered_buy_types() -> list[str]:
    return get_rule_registry().types("buy")


def registered_sell_types() -> list[str]:
    return get_rule_registry().types("sell")
