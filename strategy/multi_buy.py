"""分批买入策略 — 参数化版本

重构后:
  - 支持从 SchemeConfig 的 buy_rules 读取参数（配置驱动）
  - 无 scheme 时回退到默认参数（向后兼容）
  - 支持 support_level（交叉验证支撑位）和 trend_following（规则C）两种规则

对齐 Prompt 四维一体决策系统:
  第1批 ≤ 综合弱支撑（股息率锚/MA60/近3月低点/年内低点 的次低值）
  第2批 ≤ 综合强支撑（四者取最低值）
  第3批 ≤ 极端低估锚（每股分红 ÷ 4.0%）
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from StockInvestmentTool.core.scheme import SchemeConfig, BuyRuleConfig
from StockInvestmentTool.strategy.support import (
    IndicatorExprSource,
    MaSource,
    RollingLowSource,
    RowContext,
    build_sources,
    get_support_levels,
)

# 默认买点规则（无 scheme 时的兜底，等价于重构前的硬编码行为）
_DEFAULT_STAGES = [
    {"label": "综合弱支撑", "position_index": 1, "ratio": 0.30},
    {"label": "综合强支撑", "position_index": 0, "ratio": 0.40},
    {"label": "极端低估锚", "use_special": "dividend_anchor_4pct", "ratio": 0.30},
]
_DEFAULT_SUPPORT_SOURCES = ["dividend_anchor", "ma_60", "low_3m", "year_low"]

# 支撑位候选来源 → 可读标签（用于操作逻辑说明）
_SUPPORT_LABELS = {
    "ma_60": "MA60",
    "low_3m": "近3月低点",
    "year_low": "年内低点",
    "dividend_anchor": "股息锚",
}


class MultiBuyStrategy:
    """多批次买入计划生成（基于交叉验证支撑位）

    Args:
        scheme: 可选策略方案，提供 buy_rules 配置
        buy_ratios: 兼容旧调用，scheme 未提供时使用
        offset: 兼容旧调用，scheme 未提供时使用
        dividend_anchor: 兼容旧调用，scheme 未提供时使用
    """

    def __init__(
        self,
        buy_ratios: Optional[list[float]] = None,
        offset: float = 0.0,
        dividend_anchor: Optional[float] = None,
        scheme: Optional[SchemeConfig] = None,
        rule_params: Optional[dict] = None,
    ):
        self.scheme = scheme
        self.offset = offset
        self.dividend_anchor = dividend_anchor

        # 从 scheme 提取参数，否则回退默认
        self.buy_ratios = buy_ratios or [0.3, 0.4, 0.3]
        self.support_sources = list(_DEFAULT_SUPPORT_SOURCES)
        self.buy_stages = list(_DEFAULT_STAGES)

        if rule_params is not None:
            self._apply_support_level_params(rule_params, buy_ratios)
        elif scheme is not None:
            rule = scheme.find_buy_rule("support_level")
            if rule is not None:
                self._apply_support_level_rule(rule, buy_ratios)

    def _apply_support_level_rule(self, rule: BuyRuleConfig, buy_ratios: Optional[list]):
        """从 support_level 规则提取参数"""
        self._apply_support_level_params(rule.params or {}, buy_ratios)

    def _apply_support_level_params(self, params: dict, buy_ratios: Optional[list]):
        """Apply registry-dispatched support parameters without scheme lookup."""
        sources = params.get("support_sources")
        if isinstance(sources, list) and sources:
            self.support_sources = list(sources)

        stages = params.get("buy_stages")
        if isinstance(stages, list) and stages:
            self.buy_stages = [dict(s) for s in stages]
            self.buy_ratios = [float(s.get("ratio", 0)) for s in self.buy_stages]
        elif buy_ratios:
            self.buy_ratios = list(buy_ratios)

        # offset 支持 scheme 全局覆盖
        if "offset" in params:
            self.offset = float(params.get("offset", 0.0))

    # ── 支撑位计算 ─────────────────────────────────────

    def _collect_support_candidates(self, row: pd.Series) -> list[tuple[str, float]]:
        """收集支撑位候选值（来源key, 值）

        股息锚只作为极端低估锚（第3批），不参与强/弱支撑竞争，
        避免低息/高息股的股息锚成为不可达的"强支撑"。
        """
        candidates: list[tuple[str, float]] = []
        for source in self.support_sources:
            if source == "dividend_anchor":
                continue
            v = row.get(source) if hasattr(row, "get") else None
            if v is not None and not pd.isna(v) and v > 0:
                candidates.append((source, float(v)))
        return candidates

    @staticmethod
    def _support_label(source: str) -> str:
        return _SUPPORT_LABELS.get(source, source)

    def _get_support_levels(self, row: pd.Series) -> tuple[float, float, float]:
        """计算综合强支撑/弱支撑/极端低估锚（FR-1.2 统一骨架委托）。

        按配置的 support_sources 装配来源策略，统一走
        `strategy/support.get_support_levels` 骨架，消除三处重复算法。

        Returns
        -------
        (weak_support, strong_support, extreme_anchor)
        """
        # 兼容层：老字段名 → 来源策略；新写法（指标名/表达式）→ IndicatorExprSource
        sources, _ = build_sources(list(self.support_sources))
        return get_support_levels(
            sources, RowContext(row), row, dividend_anchor=self.dividend_anchor,
        )

    def _build_computation(self, stage: dict, base: float,
                           threshold: float, candidates: list[tuple[str, float]]) -> dict:
        """构建某批次的支撑位计算过程说明（供前端展示）"""
        if stage.get("use_special") == "dividend_anchor_4pct":
            method = "股息锚 = 每股分红 ÷ 4.0%"
        else:
            pos = stage.get("position_index")
            method = "最低值" if pos == 0 else "次低值"
        source_names = " / ".join(
            self._support_label(s) for s, _ in candidates
        ) if candidates else "—"
        return {
            "base_value": round(base, 2) if base > 0 else 0,
            "method": f"{method}（{source_names}）",
            "candidates": [
                {"source": self._support_label(s), "value": round(v, 2)}
                for s, v in sorted(candidates, key=lambda x: x[1])
            ],
            "offset": round(self.offset, 4),
            "trigger": threshold,
        }

    def _stage_threshold(
        self,
        stage: dict,
        weak: float,
        strong: float,
        extreme: float,
    ) -> float:
        """根据 stage 配置计算阈值"""
        use_special = stage.get("use_special")
        if use_special == "dividend_anchor_4pct":
            return extreme
        pos = stage.get("position_index")
        levels = [strong, weak]
        if isinstance(pos, int) and 0 <= pos < len(levels):
            return levels[pos]
        # 按 label 兜底
        label = stage.get("label", "")
        if "弱" in label:
            return weak
        if "强" in label:
            return strong
        if "极端" in label:
            return extreme
        return 0

    # ── 计划生成 ───────────────────────────────────────

    def generate_plan(self, df: pd.DataFrame, current_price: float) -> list[dict]:
        """根据最新数据生成买入计划"""
        last = df.iloc[-1]
        weak, strong, extreme = self._get_support_levels(last)
        candidates = self._collect_support_candidates(last)

        plan = []
        for i, stage in enumerate(self.buy_stages):
            base = self._stage_threshold(stage, weak, strong, extreme)
            threshold = round(base * (1 + self.offset), 2) if base > 0 else 0
            # bool() 转换避免 numpy bool 导致 JSON 序列化失败
            triggered = bool(current_price <= threshold) if threshold > 0 else False
            plan.append({
                "stage": i + 1,
                "label": stage.get("label", f"第{i+1}批"),
                "threshold": threshold,
                "ratio": float(stage.get("ratio", self.buy_ratios[i] if i < len(self.buy_ratios) else 0)),
                "triggered": triggered,
                # 计算过程说明（操作逻辑），前端据此展示"综合弱支撑从哪来"
                "computation": self._build_computation(stage, base, threshold, candidates),
            })

        return plan

    def get_ma_values(self, df: pd.DataFrame) -> dict:
        """获取各支撑位数值"""
        last = df.iloc[-1]
        weak, strong, extreme = self._get_support_levels(last)
        return {
            "综合弱支撑": round(weak, 2) if weak > 0 else None,
            "综合强支撑": round(strong, 2) if strong > 0 else None,
            "极端低估锚": round(extreme, 2) if extreme > 0 else None,
        }

    # ── 规则C: 趋势跟随买入 ─────────────────────────────

    @staticmethod
    def rule_c_triggered(
        trend: str,
        market_state: str,
        rebound_from_month_low: float,
        stock_type: str,
    ) -> tuple[bool, list[str]]:
        """规则C：四项条件全部满足时触发

        Args:
            trend: 均线排列（TechnicalIndicators.trend_judgment输出）
            market_state: 市场状态（market_state.determine_market_state输出）
            rebound_from_month_low: 从近1月低点反弹幅度(%)
            stock_type: 股票类型(A/B/C/D)

        Returns:
            (是否触发, 未满足的条件列表)
        """
        failed_conditions = []

        # ① 均线排列为多头
        if not ("多头" in trend and "MA5 > MA20 > MA60" in trend):
            failed_conditions.append("①均线非多头排列")

        # ② 市场状态为牛市初期或中期
        if market_state not in ("牛市初期", "牛市中期"):
            failed_conditions.append(f"②市场状态非牛市初/中期(当前:{market_state})")

        # ③ 从近1月低点反弹 < 10%
        if not (rebound_from_month_low < 10):
            failed_conditions.append(f"③反弹幅度过高({rebound_from_month_low:.1f}%≥10%)")

        # ④ 非强周期
        if stock_type == "C":
            failed_conditions.append("④强周期不适用规则C")

        triggered = len(failed_conditions) == 0
        return triggered, failed_conditions

    def generate_rule_c_plan(
        self,
        current_price: float,
        ma_20: float,
        weak_support: float,
    ) -> list[dict]:
        """生成规则C的建仓计划

        Prompt原文:
            第一批 当前价(立即买入) → 20%
            第二批 ≤MA20(回调至20日均线) → 30%
            第三批 ≤综合弱支撑 → 20%
            剩余仓位 → 30%（机动）
        """
        plan = [
            {"stage": 1, "label": "当前价(立即买入)", "threshold": current_price,
             "ratio": 0.20, "triggered": True},
            {"stage": 2, "label": "MA20", "threshold": round(ma_20, 2) if ma_20 > 0 else 0,
             "ratio": 0.30, "triggered": current_price <= ma_20 if ma_20 > 0 else False},
            {"stage": 3, "label": "综合弱支撑", "threshold": round(weak_support, 2) if weak_support > 0 else 0,
             "ratio": 0.20, "triggered": current_price <= weak_support if weak_support > 0 else False},
            {"stage": 4, "label": "机动仓位", "threshold": 0,
             "ratio": 0.30, "triggered": False},
        ]
        return plan
