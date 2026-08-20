"""V6.0 规则 type → 执行类 注册中心

把 v6_si_wei.yaml 里声明的新规则 type 映射到具体执行函数，
供上层引擎（analysis / backtest / 单股对比）按 type 派发。

与 v4.5 的取数方式（MultiBuyStrategy / TakeProfitOptimizer 各自
find_buy_rule/find_sell_rule）互补：这里统一登记 V6.0 新增 type，
存量 v4.5 代码不动，只做新增派发（对齐实验方案 §3.2 决策 2）。

用法:
    from StockInvestmentTool.strategy.v6_dispatch import (
        get_buy_executor, get_sell_executor, is_v6_buy_type, is_v6_sell_type)
    fn = get_buy_executor("market_state_arbiter")   # → judge_buy_tree
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from StockInvestmentTool.strategy.buy_tree import judge_buy_tree
from StockInvestmentTool.strategy.sell_tree_v6 import (
    judge_left_side, judge_logic_stop, judge_price_stop,
    judge_right_side, judge_time_stop,
)

logger = logging.getLogger(__name__)

# 买入规则 type → 执行函数
BUY_EXECUTORS: dict[str, Callable] = {
    "market_state_arbiter": judge_buy_tree,
}

# 卖出规则 type → 执行函数
SELL_EXECUTORS: dict[str, Callable] = {
    "logic_stop": judge_logic_stop,
    "price_stop": judge_price_stop,
    "time_stop": judge_time_stop,
    "left_side_fixed": judge_left_side,
    "right_side_trailing": judge_right_side,
}


def is_v6_buy_type(rule_type: str) -> bool:
    return rule_type in BUY_EXECUTORS


def is_v6_sell_type(rule_type: str) -> bool:
    return rule_type in SELL_EXECUTORS


def get_buy_executor(rule_type: str) -> Optional[Callable]:
    """按 type 取买入执行函数；未注册返回 None。"""
    fn = BUY_EXECUTORS.get(rule_type)
    if fn is None:
        logger.debug("V6 未注册买入规则 type: %s", rule_type)
    return fn


def get_sell_executor(rule_type: str) -> Optional[Callable]:
    """按 type 取卖出执行函数；未注册返回 None。"""
    fn = SELL_EXECUTORS.get(rule_type)
    if fn is None:
        logger.debug("V6 未注册卖出规则 type: %s", rule_type)
    return fn


def registered_buy_types() -> list[str]:
    return sorted(BUY_EXECUTORS)


def registered_sell_types() -> list[str]:
    return sorted(SELL_EXECUTORS)
