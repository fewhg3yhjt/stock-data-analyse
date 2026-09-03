"""止盈策略与参数网格搜索优化器 — 参数化版本

重构后:
  - TakeProfitOptimizer 支持从 SchemeConfig 的 sell_rules 读取参数（配置驱动）
  - 自由函数保留向后兼容，增加可选参数覆盖默认常量
  - 无 scheme 时回退到模块级常量（等价重构前的硬编码行为）

买入逻辑对齐 Prompt 四维一体决策系统（规则A）:
  第1批 ≤ 综合弱支撑 → 30% 底仓
  第2批 ≤ 综合强支撑 → 40% 加仓
  第3批 ≤ 极端低估锚 → 30% 极端机会

止盈逻辑 v4.5（核心重构）:
  模式一（左侧固定止盈）：价格处于前高90-100%区间时减持40%仓位
    预警区(前高×90%-95%)    → 减持20%仓位
    第一止盈区(前高×95%-100%) → 再减持20%仓位
    第二止盈区(突破前高)     → 剩余60%仓位转模式二
    若浮盈不足10%，减持比例减半

  模式二（右侧移动止盈）：突破前高后回撤跟踪
    A类(高成长)回撤≥5% → 清仓
    B类(价值白马)回撤≥3% → 清仓
    股价创新高 → 止盈线跟随上移

  左侧回撤保护（模式一与模式二之间）：
    左侧止盈后、突破前高前，剩余仓位若曾达到最小峰值利润(min_profit_for_dd)
    且收盘从峰值回撤≥drawdown_stop，则清仓，避免"左侧落袋后剩余仓位
    一路阴跌坐过山车"（600150 案例 38→30 段）。

止损对齐 Prompt:
  硬止损 = 买入均价 × (1 - 扣减率)（A/B/C类15%，D类10%）
  技术止损 = 放量跌破综合强支撑
"""

from __future__ import annotations

import logging
import os
from itertools import combinations
from typing import Optional

import numpy as np
import pandas as pd

from StockInvestmentTool.config import Config
from StockInvestmentTool.core.scheme import SchemeConfig
from StockInvestmentTool.strategy.risk_control import RiskController
from StockInvestmentTool.strategy.support import (
    MaSource,
    RollingLowSource,
    RowContext,
    get_support_levels,
)

logger = logging.getLogger(__name__)

# 状态机不变量校验开关（默认开启；STOCK_INVARIANT_CHECK=0 可关闭）
_INVARIANT_CHECK_ENABLED = os.getenv("STOCK_INVARIANT_CHECK", "1") != "0"


def _validate_position_state(
    shares: float, position_phase: str, left_tier_sold: int, label: str = "",
) -> None:
    """持仓状态不变量校验（防御性断言）。

    作用: 把"状态机被静默改坏"（如 shares>0 但 phase=='closed' 导致止盈
    失活的历史 bug）变成显式 AssertionError，而不是让回测算出一份看起来
    正常实则错误的结果。

    不变量:
      - shares≈0  ⇔  position_phase == "closed"
      - 0 <= left_tier_sold <= 2
    """
    if not _INVARIANT_CHECK_ENABLED:
        return
    tag = f"[{label}]" if label else ""
    if shares <= 1e-6:
        assert position_phase == "closed", (
            f"{tag} 不变量被违反: shares=0 但 phase={position_phase!r}（应为 'closed'）"
        )
    else:
        assert position_phase != "closed", (
            f"{tag} 不变量被违反: shares>0 但 phase='closed'，"
            f"止盈逻辑将失活（重新建仓后未重置状态机）"
        )
    assert 0 <= left_tier_sold <= 2, (
        f"{tag} 不变量被违反: left_tier_sold={left_tier_sold} 超出 [0, 2]"
    )


# ── 模块级默认常量（scheme 未提供时的兜底）───────────────────
VOLUME_SURGE_THRESHOLD = 1.8  # 技术止损：成交量超过均量多少倍视为"放量"

# 左侧止盈各档位的价格区间（基于前高的百分比）
LEFT_SIDE_ZONES = {
    "warning":   (0.90, 0.95),    # 预警区：前高90%-95%
    "tp1":       (0.95, 1.00),    # 第一止盈区：前高95%-100%
    "tp2":       (1.00, None),    # 第二止盈区：突破前高
}

LEFT_SIDE_SELL_RATIO = 0.20       # 每档减持20%仓位（共减持40%）
LEFT_SIDE_TOTAL_RATIO = 0.40      # 左侧止盈总仓位比例
LEFT_SIDE_HALF_PROFIT = 0.10      # 浮盈不足10%时减持减半

# 右侧回撤阈值按类型
RIGHT_SIDE_DRAWDOWN_THRESHOLD = {
    "A": 0.05,  # 高成长：回撤≥5%清仓
    "B": 0.03,  # 价值白马：回撤≥3%清仓
    "C": 0.05,  # 强周期：回撤≥5%
    "D": 0.05,  # 深度价值：回撤≥5%
}

# 硬止损扣减率（止损价 = 均价 × (1 - 扣减率)）
STOP_LOSS_RATES = {
    "A": 0.15,  # 高成长：均价×85%
    "B": 0.15,  # 价值白马：均价×85%
    "C": 0.15,  # 强周期：均价×85%
    "D": 0.10,  # 深度价值：均价×90%
}


def get_stop_loss_rate(stock_type: str = "B") -> float:
    """获取按类型的硬止损扣减率"""
    return STOP_LOSS_RATES.get(stock_type, 0.15)


# ═══════════════════════════════════════════════════════════════
# 模式一：左侧固定止盈
# ═══════════════════════════════════════════════════════════════

def left_side_sell_action(
    current_price: float,
    avg_cost: float,
    year_high: float,
    left_tier_sold: int,
    zones: Optional[dict] = None,
    sell_ratio: Optional[float] = None,
    half_profit: Optional[float] = None,
) -> tuple[int, float]:
    """左侧固定止盈：判定是否触发卖出

    Args:
        current_price: 当前（最高）价
        avg_cost: 持仓均价
        year_high: 近12个月最高价
        left_tier_sold: 已卖的档位数 (0, 1, 2)
        zones: 覆盖默认止盈区间 (可选)
        sell_ratio: 覆盖默认减持比例 (可选)
        half_profit: 覆盖默认"浮盈不足减半"阈值 (可选)

    Returns:
        (tier_to_sell, 卖出比例)
        tier_to_sell: 0=不卖, 1=卖预警档, 2=卖第一止盈档, 3=转右侧
        sell_fraction: 卖出的仓位比例（基于总计划仓位）
    """
    zones = zones or LEFT_SIDE_ZONES
    sell_ratio = sell_ratio if sell_ratio is not None else LEFT_SIDE_SELL_RATIO
    half_profit = half_profit if half_profit is not None else LEFT_SIDE_HALF_PROFIT

    if year_high <= 0:
        return (0, 0.0)

    pct_of_year_high = current_price / year_high
    profit_pct = (current_price - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0
    actual_ratio = sell_ratio

    # 浮盈不足时减持比例减半
    if profit_pct < half_profit:
        actual_ratio = sell_ratio * 0.5

    # 第二止盈：突破前高 (≥100%)
    if pct_of_year_high >= 1.00:
        return (3, 0.0)  # 转右侧，不在此卖出

    # 第一止盈：95%-100%
    if pct_of_year_high >= 0.95 and left_tier_sold < 2:
        return (2, actual_ratio)

    # 预警区：90%-95%
    if pct_of_year_high >= 0.90 and left_tier_sold < 1:
        return (1, actual_ratio)

    return (0, 0.0)


def left_side_sell_shares(
    tier: int,
    target_capital: float,
    current_price: float,
    left_tier_sold: int,
    avg_cost: float,
    sell_ratio: Optional[float] = None,
    half_profit: Optional[float] = None,
) -> int:
    """计算应卖出的股数

    减持20%仓位 = 卖出 target_capital × 20% 市值的股份
    """
    if tier <= left_tier_sold:
        return 0

    sell_ratio = sell_ratio if sell_ratio is not None else LEFT_SIDE_SELL_RATIO
    half_profit = half_profit if half_profit is not None else LEFT_SIDE_HALF_PROFIT

    profit_pct = (current_price - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0
    ratio = sell_ratio

    if profit_pct < half_profit:
        ratio *= 0.5

    sell_value = target_capital * ratio
    shares = int(sell_value / current_price)
    return max(shares, 0)


# ═══════════════════════════════════════════════════════════════
# 模式二：右侧移动止盈
# ═══════════════════════════════════════════════════════════════

def right_side_sell_action(
    peak_price: float,
    current_price: float,
    stock_type: str = "B",
    drawdown_by_type: Optional[dict] = None,
) -> bool:
    """右侧移动止盈：判定是否触发清仓

    从突破后的最高点回撤超过阈值时清仓

    Returns:
        True=应清仓, False=继续持有
    """
    drawdown_by_type = drawdown_by_type or RIGHT_SIDE_DRAWDOWN_THRESHOLD

    if peak_price <= 0 or current_price <= 0:
        return False

    drawdown = (peak_price - current_price) / peak_price
    threshold = drawdown_by_type.get(stock_type, 0.05)

    return drawdown >= threshold


# ═══════════════════════════════════════════════════════════════
# 价格可达性校验
# ═══════════════════════════════════════════════════════════════

def price_reachability_check(
    derived_first: float,
    derived_second: float,
    year_high: float,
) -> tuple[float, float, bool]:
    """价格可达性校验

    Args:
        derived_first: 推演第一止盈价
        derived_second: 推演第二止盈价
        year_high: 近12个月最高价

    Returns:
        (调整后第一止盈, 调整后第二止盈, 是否触发警报)
    """
    threshold = year_high * 1.05
    if derived_first > threshold or derived_second > threshold:
        # 触发警报，下调止盈价
        adjusted_first = year_high
        adjusted_second = round(year_high * 1.03, 2)
        return (adjusted_first, adjusted_second, True)

    return (derived_first, derived_second, False)


# ═══════════════════════════════════════════════════════════════
# 止盈参数搜索 + 回测引擎
# ═══════════════════════════════════════════════════════════════

class TakeProfitOptimizer:
    """止盈策略回测引擎（对齐 Prompt v4.5）

    参数来源优先级: scheme (sell_rules) > 构造器参数 > 模块常量

    Args:
        df: K线数据
        scheme: 可选策略方案（推荐），提供 sell_rules / risk / backtest 配置
        initial_cash: 初始资金
        buy_ratios: 兼容旧调用
        stock_type: 股票类型 (A/B/C/D)
        stop_loss_rate: 兼容旧调用（硬止损扣减率）
        drawdown_stop: 兼容旧调用（回撤止盈阈值）
        min_profit_for_dd: 兼容旧调用（回撤止盈最小利润）
        dividend_anchor: 兼容旧调用（股息率锚）
    """

    def __init__(
        self,
        df: pd.DataFrame,
        initial_cash: float = Config.INITIAL_CASH,
        buy_ratios: Optional[list[float]] = None,
        stock_type: str = "B",
        stop_loss_rate: float = 0.15,
        drawdown_stop: float = Config.DRAWDOWN_STOP,
        min_profit_for_dd: float = Config.MIN_PROFIT_FOR_DD,
        dividend_anchor: Optional[float] = None,
        scheme: Optional[SchemeConfig] = None,
    ):
        self.df = df.copy()
        self.initial_cash = initial_cash
        self.buy_ratios = buy_ratios or Config.BUY_RATIOS
        self.stock_type = stock_type.upper()
        self.stop_loss_rate = stop_loss_rate
        self.hard_stop_mode = "fixed"
        self.breakeven_activation = 0.08
        self.drawdown_stop = drawdown_stop
        self.min_profit_for_dd = min_profit_for_dd
        self.technical_stop_enabled = True
        self.profit_activation_enabled = False
        self.profit_activation_basis = "peak_price"
        self.min_profit_for_activation = 0.08
        self.left_profit_activation_enabled = False
        self.left_profit_activation_basis = "current_price"
        self.left_min_profit_for_activation = 0.0
        self.drawdown_protection_enabled = True
        self.drawdown_protection_min_profit = 0.08
        self.drawdown_protection_threshold = 0.10
        self.technical_support_source = "strong"
        self.dividend_anchor = dividend_anchor
        self.scheme = scheme

        # 从 scheme 提取参数（覆盖旧默认）
        if scheme is not None:
            self._apply_scheme(scheme)

        self.risk = RiskController(
            self.stop_loss_rate,
            self.drawdown_stop,
            self.min_profit_for_dd,
        )

        # 预计算滚动年度最高价（用于左侧止盈）
        self.df["year_high_rolling"] = self.df["high"].rolling(
            window=252, min_periods=60
        ).max().bfill()

    def _apply_scheme(self, scheme: SchemeConfig):
        """从方案配置提取回测/卖点参数"""
        from StockInvestmentTool.strategy.rule_registry import get_rule_registry

        registry = get_rule_registry()

        def rule_params(kind: str, rule_type: str) -> dict:
            """Read only a registered rule's params, preserving old fallback."""
            if not registry.has(kind, rule_type):
                return {}
            for configured in (scheme.buy_rules if kind == "buy" else scheme.sell_rules):
                if configured.type == rule_type:
                    return configured.params or {}
            return {}

        # 回测参数
        bt = scheme.backtest
        self.initial_cash = bt.initial_cash
        # 网格搜索参数
        if bt.grid_search is not None:
            self._trail_thresholds = list(bt.grid_search.trail_thresholds)
            self._buy_offsets = list(bt.grid_search.buy_offsets)

        # 买入比例（从 support_level 规则的 buy_stages 读取，使方案差异体现在回测中）
        params = rule_params("buy", "support_level")
        if params:
            stages = params.get("buy_stages")
            if isinstance(stages, list) and stages:
                ratios = [float(s.get("ratio", 0)) for s in stages]
                if len(ratios) == 3 and abs(sum(ratios) - 1.0) < 1e-3:
                    self.buy_ratios = ratios

        # 硬止损（扣减率）
        params = rule_params("sell", "hard_stop")
        if params:
            self.hard_stop_mode = str(params.get("mode", "fixed")).lower()
            by_type = params.get("stop_loss_by_type")
            if isinstance(by_type, dict) and by_type:
                self.stop_loss_rate = float(by_type.get(self.stock_type, self.stop_loss_rate))
            activation_by_type = params.get("breakeven_activation_by_type")
            if isinstance(activation_by_type, dict) and activation_by_type:
                self.breakeven_activation = float(
                    activation_by_type.get(self.stock_type, self.breakeven_activation)
                )

        # 左侧止盈参数
        params = rule_params("sell", "left_side_fixed")
        if params:
            self.left_profit_activation_enabled = params.get("profit_activation_enabled", False) is not False
            self.left_profit_activation_basis = str(params.get("profit_activation_basis", "current_price"))
            self.left_min_profit_for_activation = float(params.get("min_profit_for_activation", 0.0))
            zones = params.get("zones")
            if isinstance(zones, list) and zones:
                self._left_zones = {}
                for z in zones:
                    name = z.get("name")
                    rng = z.get("range", [])
                    lo = float(rng[0]) if len(rng) > 0 else 0.0
                    hi = float(rng[1]) if len(rng) > 1 else None
                    self._left_zones[name] = (lo, hi)
                self._left_sell_ratio = float(zones[0].get("sell_ratio", LEFT_SIDE_SELL_RATIO))
            if "half_profit_threshold" in params:
                self._left_half_profit = float(params["half_profit_threshold"])

        # 右侧移动止盈
        params = rule_params("sell", "right_side_trailing")
        if params:
            by_type = params.get("drawdown_by_type")
            if isinstance(by_type, dict) and by_type:
                self._right_drawdown = {k: float(v) for k, v in by_type.items()}
            if "drawdown_stop" in params:
                self.drawdown_stop = float(params["drawdown_stop"])
            if "min_profit_for_dd" in params:
                self.min_profit_for_dd = float(params["min_profit_for_dd"])
            self.drawdown_protection_enabled = params.get("drawdown_protection_enabled", True) is not False
            self.drawdown_protection_min_profit = float(params.get("drawdown_protection_min_profit", params.get("min_profit_for_dd", 0.08)))
            self.drawdown_protection_threshold = float(params.get("drawdown_protection_threshold", params.get("drawdown_stop", 0.10)))
            self.profit_activation_enabled = params.get("profit_activation_enabled", False) is not False
            self.profit_activation_basis = str(params.get("profit_activation_basis", "peak_price"))
            self.min_profit_for_activation = float(params.get("min_profit_for_activation", 0.08))

        # 技术止损
        rule = next((r for r in scheme.sell_rules if r.type == "technical_stop"), None)
        self.technical_stop_enabled = bool(scheme.risk.technical_stop_enabled)
        if rule is not None:
            params = rule.params or {}
            if "volume_surge_ratio" in params:
                self._volume_surge = float(params["volume_surge_ratio"])
            self.technical_stop_enabled = bool(
                params.get("technical_stop_enabled", scheme.risk.technical_stop_enabled)
            )
            self.technical_support_source = str(params.get("support_source", "strong"))

        # 回撤保护参数只从 right_side_trailing 规则读取；risk 保留为旧方案兼容字段。

    # ── 参数访问（带兜底）──────────────────────────────

    @property
    def _trail_thresholds(self) -> list[float]:
        return getattr(self, "__trail_thresholds", [0.03, 0.04, 0.05, 0.06, 0.08, 0.10])

    @_trail_thresholds.setter
    def _trail_thresholds(self, v):
        self.__trail_thresholds = v

    @property
    def _buy_offsets(self) -> list[float]:
        return getattr(self, "__buy_offsets", list(Config.MA_OFFSETS))

    @_buy_offsets.setter
    def _buy_offsets(self, v):
        self.__buy_offsets = v

    @property
    def _left_zones(self) -> dict:
        return getattr(self, "__left_zones", LEFT_SIDE_ZONES)

    @_left_zones.setter
    def _left_zones(self, v):
        self.__left_zones = v

    @property
    def _left_sell_ratio(self) -> float:
        return getattr(self, "__left_sell_ratio", LEFT_SIDE_SELL_RATIO)

    @_left_sell_ratio.setter
    def _left_sell_ratio(self, v):
        self.__left_sell_ratio = v

    @property
    def _left_half_profit(self) -> float:
        return getattr(self, "__left_half_profit", LEFT_SIDE_HALF_PROFIT)

    @_left_half_profit.setter
    def _left_half_profit(self, v):
        self.__left_half_profit = v

    @property
    def _right_drawdown(self) -> dict:
        return getattr(self, "__right_drawdown", RIGHT_SIDE_DRAWDOWN_THRESHOLD)

    @_right_drawdown.setter
    def _right_drawdown(self, v):
        self.__right_drawdown = v

    @property
    def _volume_surge(self) -> float:
        return getattr(self, "__volume_surge", VOLUME_SURGE_THRESHOLD)

    @_volume_surge.setter
    def _volume_surge(self, v):
        self.__volume_surge = v

    # ── 交叉验证支撑位（对齐 Prompt Part4）─────────────────

    def _get_support_levels(self, row: pd.Series) -> tuple[float, float, float]:
        """计算当前行的综合强支撑 / 弱支撑 / 极端低估锚（FR-1.2 统一骨架）

        强/弱支撑只用价格类（MA60/近3月低点/年内低点），股息锚仅作为
        极端低估锚。委托 `strategy/support.get_support_levels`，与
        multi_buy / engine_v6 同口径。

        Returns
        -------
        (weak_support, strong_support, extreme_anchor)
        """
        sources = [MaSource(60), RollingLowSource(63), RollingLowSource(None)]
        return get_support_levels(
            sources, RowContext(row), row, dividend_anchor=self.dividend_anchor,
        )

    # ── 网格搜索 ──────────────────────────────────────────

    def optimize(
        self,
        trail_thresholds: Optional[list[float]] = None,
        buy_offsets: Optional[list[float]] = None,
    ) -> tuple[tuple, float, list]:
        """网格搜索最佳右侧回撤阈值

        Returns
        -------
        (best_combo, best_return, all_results)
        """
        trail_thresholds = trail_thresholds or self._trail_thresholds
        buy_offsets = buy_offsets or self._buy_offsets

        total = len(trail_thresholds) * len(buy_offsets)
        logger.info("开始网格搜索: %d 种阈值 × %d 种偏移 = %d 种",
                    len(trail_thresholds), len(buy_offsets), total)

        best_return = -999.0
        best_combo = None
        results = []

        for offset in buy_offsets:
            for trail in trail_thresholds:
                ret = self._run_single(trail, offset)
                results.append((trail, offset, ret))
                if ret > best_return:
                    best_return = ret
                    best_combo = (trail, offset)

        logger.info("搜索完成。最优: 回撤%.1f%% 偏移%+.0f%% → %.2f%%",
                    best_combo[0] * 100, best_combo[1] * 100, best_return)
        return best_combo, best_return, results

    # ── 买入逻辑（规则A）───────────────────────────────────

    def _try_buy(self, row, offset, cash, shares, total_cost,
                 buy_stage, peak_price, sold_ratio):
        """规则A：分批买入（与止盈逻辑独立）"""
        if buy_stage >= 3:
            return cash, shares, total_cost, buy_stage, peak_price, sold_ratio, None

        low = row["low"]
        close = row["close"]
        weak, strong, extreme = self._get_support_levels(row)

        if buy_stage == 0:
            trigger = weak * (1 + offset) if weak > 0 else 0
            label = "综合弱支撑"
        elif buy_stage == 1:
            trigger = strong * (1 + offset) if strong > 0 else 0
            label = "综合强支撑"
        else:
            trigger = extreme * (1 + offset) if extreme > 0 else 0
            label = "极端低估锚"

        if trigger <= 0 or low > trigger:
            return cash, shares, total_cost, buy_stage, peak_price, sold_ratio, None

        fill_price = min(trigger, close)
        if fill_price <= 0:
            return cash, shares, total_cost, buy_stage, peak_price, sold_ratio, None

        target_amt = self.initial_cash * self.buy_ratios[buy_stage]
        buy_amt = min(cash, target_amt)
        if buy_amt <= 0:
            return cash, shares, total_cost, buy_stage, peak_price, sold_ratio, None

        buy_shares = buy_amt / fill_price
        shares += buy_shares
        total_cost += buy_shares * fill_price
        cash -= buy_amt
        buy_stage += 1
        if buy_stage == 1:
            peak_price = fill_price
            sold_ratio = 0.0

        trigger_str = f"{trigger:.2f}" if abs(offset) < 0.001 else f"{trigger:.2f}(偏移{offset:+.0%})"
        buy_info = {
            "reason": f"最低价{low:.2f}≤{label}({trigger_str})，成交价{fill_price:.2f}，触发第{buy_stage}批买入",
            "label": label, "trigger": trigger,
        }

        return cash, shares, total_cost, buy_stage, peak_price, sold_ratio, buy_info

    # ── 卖出逻辑（Prompt v4.5 重构）─────────────────────────

    def _run_sell_logic(
        self,
        row, trail_threshold, offset,
        cash, shares, total_cost,
        buy_stage, peak_price, position_phase,
        left_tier_sold, trades,
        bought_this_bar=False,
    ):
        """卖出逻辑入口（包装 _run_sell_logic_impl）。

        每次返回前校验持仓状态不变量，把状态机的隐性错误显式暴露。
        """
        result = self._run_sell_logic_impl(
            row, trail_threshold, offset,
            cash, shares, total_cost,
            buy_stage, peak_price, position_phase,
            left_tier_sold, trades,
            bought_this_bar=bought_this_bar,
        )
        _validate_position_state(result[1], result[5], result[6], label="sell_logic")
        return result

    def _run_sell_logic_impl(
        self,
        row, trail_threshold, offset,
        cash, shares, total_cost,
        buy_stage, peak_price, position_phase,
        left_tier_sold, trades,
        bought_this_bar=False,
    ):
        """统一卖出逻辑（Prompt v4.5 双模式）

        状态机: accumulating → holding → left_side → right_side → closed

        Returns
        -------
        (cash, shares, total_cost, buy_stage, peak_price,
         position_phase, left_tier_sold, triggered)
        """
        if shares <= 1e-6:
            return cash, 0.0, 0.0, 0, 0.0, "closed", 0, False

        # 重新建仓自愈: 上一轮已清仓(closed)，本轮重新买入后应重开状态机，
        # 否则左侧/右侧止盈分支因 position_phase=="closed" 全部失活（历史 bug）。
        if position_phase == "closed":
            position_phase = "accumulating"
            left_tier_sold = 0

        low, high, close = row["low"], row["high"], row["close"]
        avg_cost = total_cost / shares
        year_high = row.get("year_high_rolling", row.get("year_high", close))

        # ── ① 技术止损：最低价跌破强支撑 + 放量 ──
        weak, strong, extreme = self._get_support_levels(row)
        volume = row.get("volume", 0)
        row_index = self.df.index.get_loc(row.name) if row.name in self.df.index else -1
        previous_vol_ma5 = 0.0
        if row_index >= 5:
            previous_vol_ma5 = float(self.df["volume"].iloc[row_index - 5:row_index].mean())
        vol_surge = previous_vol_ma5 > 0 and volume > previous_vol_ma5 * self._volume_surge
        support = {"strong": strong, "weak": weak, "ma60": row.get("ma60", 0)}.get(
            self.technical_support_source, strong
        ) or 0
        if self.technical_stop_enabled and support > 0 and low < support and vol_surge:
            fill = close
            cash += shares * fill
            trades.append({
                "date": row["date"], "type": "技术止损(清仓)",
                "price": round(fill, 2), "shares": round(shares, 2),
                "amount": round(shares * fill, 2),
                "pnl": round(shares * (fill - avg_cost), 2),
                "reason": f"最低价{low:.2f}放量跌破{self.technical_support_source}支撑线({support:.2f})，触发技术止损（成交量/前5日均量={volume / previous_vol_ma5:.2f}倍）",
            })
            return cash, 0.0, 0.0, 0, 0.0, "closed", 0, True

        # ── ② 更新最高价（用于右侧移动止盈） ──
        if close > peak_price:
            peak_price = close

        # ── ③ 硬止损：固定比例止损或按成本反推保本止损价 ──
        breakeven_activated = self.hard_stop_mode == "breakeven"
        stop_price = (
            avg_cost / (1 + self.breakeven_activation)
            if breakeven_activated else avg_cost * (1 - self.stop_loss_rate)
        )
        if low <= stop_price:
            fill = close
            cash += shares * fill
            trades.append({
                "date": row["date"], "type": "硬止损(清仓)",
                "price": round(fill, 2), "shares": round(shares, 2),
                "amount": round(shares * fill, 2),
                "pnl": round(shares * (fill - avg_cost), 2),
                "reason": (
                    f"最低价{low:.2f}≤保本止损价{stop_price:.2f}（成本÷(1+{self.breakeven_activation:.1%})）"
                    if breakeven_activated else
                    f"最低价{low:.2f}≤固定止损线{stop_price:.2f}（成本{avg_cost:.2f}×{1-self.stop_loss_rate:.0%})"
                ),
            })
            return cash, 0.0, 0.0, 0, 0.0, "closed", 0, True

        # ── ④ 左侧固定止盈（前高90-100%区间） ──
        # left_side 阶段也继续检查更高档位（95-100% 第一止盈区），
        # 否则第一档卖完后第二档会被漏判。
        # 当日刚买入(bought_this_bar)时不触发止盈，避免"刚在低点买、当天
        # 高点又卖"的同bar矛盾。
        if not bought_this_bar and position_phase in ("accumulating", "holding", "left_side"):
            activation_value = peak_price if self.left_profit_activation_basis == "peak_price" else close
            left_profit_ready = (
                not self.left_profit_activation_enabled
                or activation_value >= avg_cost * (1 + self.left_min_profit_for_activation)
            )
            if not left_profit_ready:
                return cash, shares, total_cost, buy_stage, peak_price, position_phase, left_tier_sold, False
            tier, _ = left_side_sell_action(
                current_price=high,
                avg_cost=avg_cost,
                year_high=year_high,
                left_tier_sold=left_tier_sold,
                zones=self._left_zones,
                sell_ratio=self._left_sell_ratio,
                half_profit=self._left_half_profit,
            )

            if tier > 0 and tier <= 2:
                # 卖出指定股数
                sell_shares = left_side_sell_shares(
                    tier=tier,
                    target_capital=self.initial_cash,
                    current_price=high,
                    left_tier_sold=left_tier_sold,
                    avg_cost=avg_cost,
                    sell_ratio=self._left_sell_ratio,
                    half_profit=self._left_half_profit,
                )
                sell_shares = min(sell_shares, int(shares * 0.5))  # 安全限制：单次不超过50%

                if sell_shares > 5:
                    fill = high  # 限价止盈
                    cash += sell_shares * fill
                    total_cost *= max(0, 1 - sell_shares / max(shares, 1e-10))
                    shares -= sell_shares
                    left_tier_sold = tier

                    tier_label = {1: "预警区", 2: "第一止盈区"}.get(tier, f"第{tier}档")
                    trades.append({
                        "date": row["date"],
                        "type": f"左侧止盈({tier_label})",
                        "price": round(fill, 2),
                        "shares": round(sell_shares, 2),
                        "amount": round(sell_shares * fill, 2),
                        "pnl": round(sell_shares * (fill - avg_cost), 2),
                        "reason": f"最高价{high:.2f}达前高{year_high:.2f}的{high/year_high*100:.1f}%，"
                                   f"触发左侧{tier_label}，卖出{sell_shares:.0f}股",
                    })

                    if shares <= 1e-6:
                        return cash, 0.0, 0.0, 0, 0.0, "closed", 2, True

                    position_phase = "left_side"
                    return cash, shares, total_cost, buy_stage, peak_price, position_phase, left_tier_sold, True

            # 突破前高 → 转右侧移动止盈（不再要求左侧已卖出，
            # 否则跳空/快速突破时永远不会进入右侧跟踪）
            if tier == 3:
                position_phase = "right_side"

        # ── ④' 左侧回撤保护：未突破前高时，从峰值回撤过大则清仓 ──
        # 仅 left_side 阶段（已卖过至少一档左侧止盈）生效，解决"左侧止盈后
        # 剩余仓位在突破前无任何保护、一路阴跌坐过山车"的问题。
        # 触发条件: 本次持仓曾达到最小峰值利润 + 收盘从峰值回撤超过阈值。
        # 用峰值利润（而非当前利润）判定，保证"曾经赚过就锁住"，避免刚建仓
        # 没盈利的仓位被无谓地回撤止盈。
        if not bought_this_bar and position_phase == "left_side" and peak_price > 0:
            peak_profit = (peak_price - avg_cost) / avg_cost if avg_cost > 0 else 0.0
            dd_pct = (peak_price - close) / peak_price
            if (self.drawdown_protection_enabled and
                    peak_profit >= self.drawdown_protection_min_profit and
                    dd_pct >= self.drawdown_protection_threshold):
                fill = close
                cash += shares * fill
                trades.append({
                    "date": row["date"], "type": "左侧回撤止盈(清仓)",
                    "price": round(fill, 2), "shares": round(shares, 2),
                    "amount": round(shares * fill, 2),
                    "pnl": round(shares * (fill - avg_cost), 2),
                    "reason": f"左侧止盈后从峰值{peak_price:.2f}回撤{dd_pct:.1%}"
                               f"（阈值{self.drawdown_protection_threshold:.0%}），触发左侧回撤保护清仓",
                })
                return cash, 0.0, 0.0, 0, 0.0, "closed", left_tier_sold, True

        # ── ⑤ 右侧移动止盈（突破后回撤跟踪） ──
        if not bought_this_bar and position_phase in ("left_side", "right_side"):
            # 已突破前高 → 检查回撤
            if high >= year_high:
                position_phase = "right_side"

            if position_phase == "right_side":
                activation_value = peak_price if self.profit_activation_basis == "peak_price" else close
                profit_ready = (not self.profit_activation_enabled or
                                activation_value >= avg_cost * (1 + self.min_profit_for_activation))
                # ``trail_threshold`` is the optimizer's per-run parameter.
                # Apply it to the current stock type instead of only printing
                # it in the reason text; scheme defaults remain the fallback.
                drawdown_by_type = dict(self._right_drawdown or {})
                drawdown_by_type[self.stock_type] = float(trail_threshold)
                should_sell = profit_ready and right_side_sell_action(
                    peak_price=peak_price,
                    current_price=close,
                    stock_type=self.stock_type,
                    drawdown_by_type=drawdown_by_type,
                )
                if should_sell:
                    dd_pct = (peak_price - close) / peak_price * 100
                    fill = close
                    cash += shares * fill
                    trades.append({
                        "date": row["date"],
                        "type": "右侧止盈(移动清仓)",
                        "price": round(fill, 2),
                        "shares": round(shares, 2),
                        "amount": round(shares * fill, 2),
                        "pnl": round(shares * (fill - avg_cost), 2),
                        "reason": f"突破前高后从{peak_price:.2f}回撤{dd_pct:.1f}%"
                                   f"（阈值{self.stock_type}类{trail_threshold*100:.0f}%），触发右侧清仓",
                    })
                    return cash, 0.0, 0.0, 0, 0.0, "closed", left_tier_sold, True

        return cash, shares, total_cost, buy_stage, peak_price, position_phase, left_tier_sold, False

    # ── 单次回测（快速）─────────────────────────────────

    def _run_single(self, trail_threshold: float = 0.05,
                    offset: float = 0.0) -> float:
        """单次回测，只返回总收益率"""
        cash = self.initial_cash
        shares = 0.0
        total_cost = 0.0
        buy_stage = 0
        peak_price = 0.0
        position_phase = "accumulating"
        left_tier_sold = 0
        trades_dummy: list = []

        for i in range(len(self.df)):
            row = self.df.iloc[i]

            # 买入（规则A）
            bought_this_bar = False
            if buy_stage < 3:
                (cash, shares, total_cost, buy_stage,
                 peak_price, _, buy_info) = self._try_buy(
                    row, offset, cash, shares, total_cost,
                    buy_stage, peak_price, 0.0,
                )
                bought_this_bar = buy_info is not None

            # 卖出（Prompt v4.5 双模式）
            cash, shares, total_cost, buy_stage, peak_price, \
                position_phase, left_tier_sold, triggered = self._run_sell_logic(
                row, trail_threshold, offset,
                cash, shares, total_cost,
                buy_stage, peak_price, position_phase,
                left_tier_sold, trades_dummy,
                bought_this_bar=bought_this_bar,
            )

        if shares > 0:
            cash += shares * self.df["close"].iloc[-1]

        return (cash - self.initial_cash) / self.initial_cash * 100

    # ── 详细回测（含明细）─────────────────────────────────

    def run_detailed(self, trail_threshold: float = 0.05,
                     offset: float = 0.0,
                     trade_start_date: Optional[str] = None) -> dict:
        """用指定参数完整回测，返回交易明细 + 权益曲线 + 绩效"""
        cash = self.initial_cash
        shares = 0.0
        total_cost = 0.0
        buy_stage = 0
        peak_price = 0.0
        position_phase = "accumulating"
        left_tier_sold = 0

        trades: list[dict] = []
        equity_curve: list[dict] = []

        for i in range(len(self.df)):
            row = self.df.iloc[i]
            date = row["date"]
            close = row["close"]

            # 建仓日前只提供均线/滚动指标预热，不参与模拟交易或权益曲线。
            if trade_start_date and str(date)[:10] < str(trade_start_date)[:10]:
                continue

            # ── 买入（规则A）──
            bought_this_bar = False
            if buy_stage < 3:
                (cash, shares, total_cost, buy_stage,
                 peak_price, _, buy_info) = self._try_buy(
                    row, offset, cash, shares, total_cost,
                    buy_stage, peak_price, 0.0,
                )
                if buy_info:
                    bought_this_bar = True
                    fill_price = buy_info.get("trigger", close)
                    trades.append({
                        "date": date,
                        "type": f"买入第{buy_stage}批",
                        "price": round(fill_price, 2),
                        "shares": round((self.initial_cash * self.buy_ratios[buy_stage - 1]) / fill_price, 2),
                        "amount": round(self.initial_cash * self.buy_ratios[buy_stage - 1], 2),
                        "reason": buy_info.get("reason", ""),
                    })

            # ── 卖出（Prompt v4.5）──
            cash, shares, total_cost, buy_stage, peak_price, \
                position_phase, left_tier_sold, triggered = self._run_sell_logic(
                row, trail_threshold, offset,
                cash, shares, total_cost,
                buy_stage, peak_price, position_phase,
                left_tier_sold, trades,
                bought_this_bar=bought_this_bar,
            )
            if triggered:
                equity_curve.append({
                    "date": date,
                    "total_asset": round(cash + shares * close, 2),
                    "price": round(close, 2),
                })
                continue

            # 每日净资产
            total_asset = cash + shares * close
            equity_curve.append({
                "date": date, "total_asset": round(total_asset, 2), "price": round(close, 2),
            })

        # 期末平仓
        if shares > 0:
            final_price = self.df["close"].iloc[-1]
            cash += shares * final_price
            avg_cost_final = total_cost / shares if shares > 0 else 0
            trades.append({
                "date": self.df["date"].iloc[-1],
                "type": "期末平仓",
                "price": round(final_price, 2),
                "shares": round(shares, 2),
                "amount": round(shares * final_price, 2),
                "pnl": round(shares * (final_price - avg_cost_final), 2),
                "reason": "回测期结束，强制平仓",
            })
            shares = 0.0

        final_asset = cash
        total_return = (final_asset - self.initial_cash) / self.initial_cash * 100
        buy_hold_return = (self.df["close"].iloc[-1] / self.df["close"].iloc[0] - 1) * 100

        # Expose the same event-derived metric used by live Advisor.  The
        # frame is the historical visible window, so no future rows leak into
        # each reported buy event.
        post_metrics = []
        from StockInvestmentTool.portfolio.trade_metrics import calculate_post_metrics
        for trade in trades:
            if not str(trade.get("type", "")).startswith("买入"):
                continue
            metric = calculate_post_metrics(
                buy_date=str(trade["date"])[:10], buy_price=trade["price"],
                daily=self.df, as_of=self.df["date"].iloc[-1],
            )
            post_metrics.append({"buy_date": str(trade["date"])[:10],
                                 "buy_price": trade["price"], **metric.to_dict()})

        return {
            "initial_cash": self.initial_cash,
            "final_asset": round(final_asset, 2),
            "total_return": round(total_return, 2),
            "buy_hold_return": round(buy_hold_return, 2),
            "excess_return": round(total_return - buy_hold_return, 2),
            "trades": trades,
            "trade_count": len(trades),
            "equity_curve": equity_curve,
            "post_metrics": post_metrics,
            "params": {"trail_threshold": trail_threshold, "offset": offset},
        }
