"""内置规则 executor 注册表构建（FR-1.1）

把 SRD §FR-1.1 列出的全部 rule type 注册进 RuleRegistry。
此处 executor 是「适配器」：把 RuleContext + params 转成既有引擎函数所需的
调用形式，保证 v4.5 / V6.0 算法语义不变（SRD §8-1）。

涵盖 type（SRD §4 FR-1.1）：
  买入:  support_level, trend_following (v4.5) + market_state_arbiter (V6.0)
  卖出:  hard_stop, technical_stop, left_side_fixed, right_side_trailing (v4.5)
        + logic_stop, price_stop, time_stop, left_side_fixed, right_side_trailing (V6.0)
"""

from __future__ import annotations

from typing import Any

from StockInvestmentTool.strategy.context import RuleContext, RuleResult
from StockInvestmentTool.strategy.rule_registry import (
    KIND_BUY,
    KIND_SELL,
    ParamField,
    RuleExecutor,
)


# ── 买入规则 ───────────────────────────────────────────────

def _execute_support_level(ctx: RuleContext, params: dict) -> RuleResult:
    """support_level：交叉验证支撑位买入（v4.5 MultiBuyStrategy）。

    实际执行委托给 MultiBuyStrategy.generate_plan，返回批次计划。
    """
    from StockInvestmentTool.strategy.multi_buy import MultiBuyStrategy

    scheme = ctx.extra.get("scheme")
    strategy = MultiBuyStrategy(
        dividend_anchor=ctx.dividend_anchor,
        scheme=scheme,
        rule_params=params,
    )
    plan = strategy.generate_plan(ctx.df, ctx.current_price)
    return RuleResult(
        triggered=any(p.get("triggered") for p in plan),
        action="buy_more",
        detail={"plan": plan, "rule": "support_level"},
    )


def _execute_trend_following(ctx: RuleContext, params: dict) -> RuleResult:
    """trend_following：规则C 趋势跟随买入（v4.5）。

    依赖 trend / market_state / rebound_from_month_low，从 extra 取。
    """
    from StockInvestmentTool.strategy.multi_buy import MultiBuyStrategy

    trend = ctx.extra.get("trend", "")
    market_state = ctx.extra.get("market_state", "")
    rebound_from_month_low = ctx.extra.get("rebound_from_month_low", 999.0)
    stock_type = ctx.extra.get("stock_type", "B")
    triggered, failed = MultiBuyStrategy.rule_c_triggered(
        trend, market_state, rebound_from_month_low, stock_type
    )
    return RuleResult(
        triggered=triggered,
        action="buy_more",
        reason="规则C趋势跟随触发" if triggered else f"规则C未满足: {failed}",
        detail={"failed": failed, "rule": "trend_following"},
    )


def _execute_market_state_arbiter(ctx: RuleContext, params: dict) -> RuleResult:
    """market_state_arbiter：V6.0 市场状态仲裁买入（judge_buy_tree 纯函数）。"""
    from StockInvestmentTool.strategy.buy_tree import judge_buy_tree, BRANCH_FORBID

    res = judge_buy_tree(
        market_state=ctx.market_state or ctx.extra.get("market_state", ""),
        stock_type=ctx.extra.get("stock_type", "B"),
        price=ctx.current_price,
        ma20=ctx.extra.get("ma20"),
        weak_support=ctx.extra.get("weak_support"),
        strong_support=ctx.extra.get("strong_support"),
        extreme_anchor=ctx.dividend_anchor,
        bias_ratio=ctx.extra.get("bias_ratio"),
        screener_pass=ctx.extra.get("screener_pass", True),
        position_below_macro_cap=ctx.extra.get("position_below_macro_cap", True),
        circuit_breaker_ok=ctx.extra.get("circuit_breaker_ok", True),
        valuation_ok=ctx.extra.get("valuation_ok", True),
        thesis_ok=ctx.extra.get("thesis_ok", True),
    )
    blocked = res.blocked or res.branch == BRANCH_FORBID
    return RuleResult(
        triggered=(not blocked and bool(res.batches)),
        action="buy_more" if not blocked else "hold",
        reason=res.reason,
        detail={
            "branch": res.branch,
            "batches": res.batches,
            "position_pct": res.position_pct,
            "blocked_by": res.blocked_by,
            "rule": "market_state_arbiter",
        },
    )


# ── 卖出规则（适配既有判定函数）────────────────────────────

def _execute_hard_stop(ctx: RuleContext, params: dict) -> RuleResult:
    """hard_stop：硬止损（v4.5，均价 × (1 - 扣减率)）。"""
    if ctx.avg_cost <= 0:
        return RuleResult(triggered=False, action="hold", reason="无持仓成本")
    by_type = params.get("stop_loss_by_type") or {}
    rate = float(by_type.get(ctx.extra.get("stock_type", "B"), 0.15))
    stop = ctx.avg_cost * (1 - rate)
    low = float(ctx.row.get("low", ctx.current_price)) if ctx.row is not None else ctx.current_price
    triggered = low <= stop
    return RuleResult(
        triggered=triggered,
        action="clear" if triggered else "hold",
        reason=f"最低价{low:.2f} ≤ 止损线{stop:.2f}（成本{ctx.avg_cost:.2f}×{1-rate:.0%})"
               if triggered else f"未破硬止损{stop:.2f}",
        detail={"stop_price": round(stop, 2), "low": round(low, 2), "rule": "hard_stop"},
    )


def _execute_technical_stop(ctx: RuleContext, params: dict) -> RuleResult:
    """technical_stop：技术止损（放量跌破强支撑）。"""
    surge_th = float(params.get("volume_surge_ratio", 1.8))
    vol_ma = ctx.row.get("vol_ma5", 0) if ctx.row is not None else 0
    vol = ctx.row.get("volume", 0) if ctx.row is not None else 0
    vol_surge = (vol_ma > 0 and vol > vol_ma * surge_th)
    strong = ctx.extra.get("strong_support", 0) or 0
    low = float(ctx.row.get("low", ctx.current_price)) if ctx.row is not None else ctx.current_price
    triggered = strong > 0 and low < strong and vol_surge
    return RuleResult(
        triggered=triggered,
        action="clear" if triggered else "hold",
        reason=f"最低价{low:.2f}放量跌破强支撑{strong:.2f}" if triggered else "技术止损未触发",
        detail={"strong_support": round(strong, 2), "low": round(low, 2),
                "volume_surge": vol_surge, "rule": "technical_stop"},
    )


def _execute_left_side_fixed(ctx: RuleContext, params: dict) -> RuleResult:
    """left_side_fixed：左侧固定止盈（前高分档减持）。"""
    from StockInvestmentTool.strategy.take_profit import left_side_sell_action

    zones = None
    sell_ratio = None
    half_profit = None
    z = params.get("zones")
    if isinstance(z, list) and z:
        zones = {}
        for item in z:
            rng = item.get("range", [])
            lo = float(rng[0]) if len(rng) > 0 else 0.0
            hi = float(rng[1]) if len(rng) > 1 and rng[1] is not None else None
            zones[item.get("name", "zone")] = (lo, hi)
        sell_ratio = float(z[0].get("sell_ratio", 0.20))
    if "half_profit_threshold" in params:
        half_profit = float(params["half_profit_threshold"])

    high = float(ctx.row.get("high", ctx.current_price)) if ctx.row is not None else ctx.current_price
    tier, ratio = left_side_sell_action(
        current_price=high, avg_cost=ctx.avg_cost, year_high=ctx.year_high,
        left_tier_sold=ctx.left_tier_sold,
        zones=zones, sell_ratio=sell_ratio, half_profit=half_profit,
    )
    action = "partial_sell" if tier in (1, 2) else ("transition" if tier == 3 else "hold")
    return RuleResult(
        triggered=tier in (1, 2, 3),
        action=action,
        reason=f"左侧止盈档位{tier}" if tier else "未达左侧止盈区间",
        detail={"tier": tier, "sell_ratio": ratio, "year_high": ctx.year_high,
                "rule": "left_side_fixed"},
    )


def _execute_right_side_trailing(ctx: RuleContext, params: dict) -> RuleResult:
    """right_side_trailing：右侧移动止盈（突破后回撤清仓）。"""
    from StockInvestmentTool.strategy.take_profit import right_side_sell_action

    dd = params.get("drawdown_by_type") or {}
    stock_type = ctx.extra.get("stock_type", "B")
    should_sell = right_side_sell_action(
        peak_price=ctx.peak_price, current_price=ctx.current_price,
        stock_type=stock_type, drawdown_by_type=dd or None,
    )
    return RuleResult(
        triggered=should_sell,
        action="clear" if should_sell else "hold",
        reason=f"从峰值{ctx.peak_price:.2f}回撤，触发右侧清仓" if should_sell else "右侧移动止盈未触发",
        detail={"peak_price": ctx.peak_price, "current": ctx.current_price,
                "rule": "right_side_trailing"},
    )


def _execute_logic_stop(ctx: RuleContext, params: dict) -> RuleResult:
    """logic_stop：V6.0 逻辑止损（thesis 被证伪）。"""
    from StockInvestmentTool.strategy.sell_tree_v6 import judge_logic_stop

    thesis_ok = ctx.extra.get("thesis_ok")
    d = judge_logic_stop(thesis_ok=thesis_ok)
    return RuleResult(
        triggered=d is not None,
        action="clear" if d and d.action == "clear" else "hold",
        reason=d.reason if d else "逻辑止损未触发",
        detail={"rule": "logic_stop"},
    )


def _execute_price_stop(ctx: RuleContext, params: dict) -> RuleResult:
    """price_stop：V6.0 价格止损 + 贝塔保护。"""
    from StockInvestmentTool.strategy.sell_tree_v6 import judge_price_stop

    low = float(ctx.row.get("low", ctx.current_price)) if ctx.row is not None else ctx.current_price
    d = judge_price_stop(
        stock_type=ctx.extra.get("stock_type", "B"),
        low=low, avg_cost=ctx.avg_cost,
        csi300_drop_pct=ctx.extra.get("csi300_drop_pct"),
        erp=ctx.extra.get("erp"),
        left_tp_triggered=ctx.left_tier_sold > 0,
    )
    if d is None:
        return RuleResult(triggered=False, action="hold", reason="价格止损未触发",
                          detail={"rule": "price_stop"})
    return RuleResult(
        triggered=d.action == "clear",
        action=d.action,
        reason=d.reason,
        detail={"rule": "price_stop", **d.detail},
    )


def _execute_time_stop(ctx: RuleContext, params: dict) -> RuleResult:
    """time_stop：V6.0 时间止损。"""
    from StockInvestmentTool.strategy.sell_tree_v6 import judge_time_stop

    d = judge_time_stop(
        stock_type=ctx.extra.get("stock_type", "B"),
        holding_days=ctx.extra.get("holding_days", 0),
        right_signal_occurred=ctx.extra.get("right_signal_occurred"),
        revenue_accel=ctx.extra.get("revenue_accel"),
    )
    if d is None:
        return RuleResult(triggered=False, action="hold", reason="时间止损未触发",
                          detail={"rule": "time_stop"})
    return RuleResult(
        triggered=True,
        action="partial_sell",
        reason=d.reason,
        detail={"rule": "time_stop", "ratio": d.ratio, **d.detail},
    )


# ── schema 定义 ────────────────────────────────────────────

def _build_schemas() -> dict[str, list[ParamField]]:
    """各 rule type 的参数元数据（供前端动态表单，FR-2.2）。"""
    s: dict[str, list[ParamField]] = {}

    s["support_level"] = [
        ParamField("support_sources", "支撑源", "list",
                   default=["MA60", "MIN(MA20,MA240)"], required=True,
                   help="候选支撑位来源，可引用指标名/表达式，多个取次低=弱支撑、最低=强支撑"),
        ParamField("buy_stages", "买入批次", "map_list",
                   default=[
                       {"label": "综合弱支撑", "position_index": 1, "ratio": 0.3},
                       {"label": "综合强支撑", "position_index": 0, "ratio": 0.4},
                       {"label": "极端低估锚", "use_special": "dividend_anchor_4pct", "ratio": 0.3},
                   ], required=True,
                   help="每一批的阈值归属与仓位比例（比例合计应为 1.0）"),
        ParamField("offset", "买入偏移", "number", default=0.0, min=-0.2, max=0.2,
                   help="阈值整体偏移比例，正数放宽（更容易触发）"),
    ]

    s["trend_following"] = [
        ParamField("rebound_limit_pct", "反弹幅度上限%", "number", default=10, max=100),
        ParamField("require_market", "市场状态要求", "select",
                   default="牛市初期",
                   options=["牛市初期", "牛市中期", "牛市初期/中期"]),
    ]

    s["market_state_arbiter"] = [
        ParamField("screener_pass", "六步法通过", "select", default=True,
                   options=[True, False]),
        ParamField("circuit_breaker_ok", "年线熔断放行", "select", default=True,
                   options=[True, False]),
    ]

    s["hard_stop"] = [
        ParamField("stop_loss_by_type", "按类型扣减率", "map",
                   default={"A": 0.15, "B": 0.15, "C": 0.15, "D": 0.10},
                   help="止损价 = 均价 × (1 - 扣减率)"),
    ]

    s["technical_stop"] = [
        ParamField("volume_surge_ratio", "放量倍数", "number", default=1.8, min=1.0),
        ParamField("technical_stop_enabled", "启停", "select", default=True,
                   options=[True, False]),
    ]

    s["left_side_fixed"] = [
        ParamField("year_high_window", "前高回看交易日数", "number", default=252, min=20, max=2000,
                   help="在最近多少个交易日内寻找前高；默认 252 日约一年"),
        ParamField("year_high_price_field", "前高计算价格字段", "select", default="high",
                   options=["high", "close"], help="用每日最高价或收盘价计算滚动前高"),
        ParamField("zones", "止盈区间", "map_list",
                   default=[
                       {"name": "预警区", "range": [0.90, 0.95], "sell_ratio": 0.20},
                       {"name": "第一止盈区", "range": [0.95, 1.00], "sell_ratio": 0.20},
                   ],
                   help="区间为「前高的比例」"),
        ParamField("half_profit_threshold", "浮盈减半阈值", "number", default=0.10),
        ParamField("ratio_by_type", "按类型总减持比例", "map",
                   default={"A": 0.20, "B": 0.40, "C": 0.60, "D": 0.40}),
    ]

    s["right_side_trailing"] = [
        ParamField("drawdown_by_type", "按类型回撤阈值", "map",
                   default={"A": 0.05, "B": 0.03, "C": 0.05, "D": 0.03}),
    ]

    s["logic_stop"] = [
        ParamField("thesis_ok", "逻辑是否成立", "select", default=True,
                   options=[True, False]),
    ]

    s["price_stop"] = [
        ParamField("base_discount", "基础扣减率", "number", default=0.15),
    ]

    s["time_stop"] = [
        ParamField("holding_days_limit", "持有天数上限", "number", default=180),
        ParamField("grace_days", "赦免期", "number", default=60),
    ]
    return s


# ── 构建函数 ───────────────────────────────────────────────

def build_builtin_executors() -> list[RuleExecutor]:
    """构造全部内置规则 executor（供注册中心使用）。"""
    schemas = _build_schemas()
    executors = [
        # 买入
        RuleExecutor(KIND_BUY, "support_level", _execute_support_level,
                     schemas["support_level"], "交叉验证支撑位分批买入"),
        RuleExecutor(KIND_BUY, "trend_following", _execute_trend_following,
                     schemas["trend_following"], "规则C趋势跟随加仓"),
        RuleExecutor(KIND_BUY, "market_state_arbiter", _execute_market_state_arbiter,
                     schemas["market_state_arbiter"], "V6.0市场状态仲裁买入"),
        # 卖出
        RuleExecutor(KIND_SELL, "hard_stop", _execute_hard_stop,
                     schemas["hard_stop"], "硬止损"),
        RuleExecutor(KIND_SELL, "technical_stop", _execute_technical_stop,
                     schemas["technical_stop"], "技术止损（放量破强支撑）"),
        RuleExecutor(KIND_SELL, "left_side_fixed", _execute_left_side_fixed,
                     schemas["left_side_fixed"], "左侧固定止盈"),
        RuleExecutor(KIND_SELL, "right_side_trailing", _execute_right_side_trailing,
                     schemas["right_side_trailing"], "右侧移动止盈"),
        RuleExecutor(KIND_SELL, "logic_stop", _execute_logic_stop,
                     schemas["logic_stop"], "V6.0逻辑止损"),
        RuleExecutor(KIND_SELL, "price_stop", _execute_price_stop,
                     schemas["price_stop"], "V6.0价格止损+贝塔保护"),
        RuleExecutor(KIND_SELL, "time_stop", _execute_time_stop,
                     schemas["time_stop"], "V6.0时间止损"),
    ]
    return executors
