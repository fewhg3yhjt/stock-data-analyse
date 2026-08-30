"""支撑位骨架 + 来源策略（FR-1.2 去重）

设计意图（HLD §3.2.1 / 模板方法 + 策略 + 工厂 / ADR-5）：
  - 三处重复的支撑位算法（multi_buy / take_profit / engine_v6）真正相同的是
    骨架「收集候选 → 排序 → 取 强/弱/锚」，不同的是**候选来源**；
  - 沿「骨架 vs 来源」切分，而不是合并成一个固定函数：
      * 骨架 `get_support_levels()` 唯一实现；
      * 来源用 `SupportSource` 策略体系（MaSource / RollingLowSource / IndicatorExprSource）；
      * `SUPPORT_SOURCE_FACTORY` 做老 yaml 字段名 → 来源策略的兼容映射；
  - 未来 `support_sources: ["MIN(MA20,MA240)"]` 直接走 `IndicatorExprSource`，
    来源退化为「指标求值」。

口径约定（与回测 year_high 一致）：候选均要求 > 0；股息锚不作为强/弱候选，
仅作为极端低估锚（extreme）。
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

import pandas as pd


class SupportSource(Protocol):
    """支撑位候选来源策略。"""

    def compute(self, ctx: Any, row: Optional[pd.Series] = None) -> float:
        """返回候选值；无有效值返回 None/0。"""
        ...


class MaSource:
    """均线来源：优先用 row 预计算列（保持 compute_all 舍入口径），否则 ctx.ma(window)。"""

    def __init__(self, window: int):
        self.window = int(window)

    def compute(self, ctx: Any, row: Optional[pd.Series] = None) -> float:
        # 老算法读 row 的 ma{window}（compute_all 已 round(2)），保持回测口径一致
        if row is not None and f"ma{self.window}" in row.index:
            v = row.get(f"ma{self.window}")
        else:
            try:
                v = ctx.ma(self.window)
            except Exception:
                return 0.0
        return float(v) if v is not None and not pd.isna(v) and v > 0 else 0.0


class RollingLowSource:
    """滚动最低价来源：row 预计算列（low_3m/year_low）优先，否则 ctx.rolling_low(window)。

    window=None 表示年内/全历史最低（year_low）。
    """

    def __init__(self, window: Optional[int] = None):
        self.window = window

    def compute(self, ctx: Any, row: Optional[pd.Series] = None) -> float:
        if row is not None:
            key = "low_3m" if self.window and self.window <= 63 else "year_low"
            if key in row.index:
                v = row.get(key)
                if v is not None and not pd.isna(v) and v > 0:
                    return float(v)
        try:
            v = ctx.rolling_low(self.window)
        except Exception:
            return 0.0
        return float(v) if v is not None and not pd.isna(v) and v > 0 else 0.0


class IndicatorExprSource:
    """任意指标表达式来源：ctx.eval(expr)"""

    def __init__(self, expr: str):
        self.expr = expr

    def compute(self, ctx: Any, row: Optional[pd.Series] = None) -> float:
        try:
            v = ctx.eval(self.expr)
        except Exception:
            return 0.0
        return float(v) if v is not None and not pd.isna(v) and v > 0 else 0.0


# ── 骨架（唯一实现）────────────────────────────────────────

def get_support_levels(
    sources: list[Any],
    ctx: Any,
    row: Optional[pd.Series] = None,
    dividend_anchor: Optional[float] = None,
) -> tuple[float, float, float]:
    """计算 (weak_support, strong_support, extreme_anchor)。

    骨架：收集候选 → 过滤有效值 → 升序排序 →
          strong=最低（第1名），weak=次低（第2名），extreme=股息锚或 strong。
    """
    cands: list[float] = []
    for s in sources:
        try:
            v = s.compute(ctx, row)
        except Exception:
            continue
        if v is not None and not pd.isna(v) and v > 0:
            cands.append(float(v))
    if not cands:
        return (0.0, 0.0, 0.0)
    cands.sort()
    strong = cands[0]
    weak = cands[1] if len(cands) > 1 else strong
    extreme = float(dividend_anchor) if dividend_anchor and dividend_anchor > 0 else strong
    return (weak, strong, extreme)


# ── yaml 字段名 → 来源策略（兼容层：新老命名均可解析）────────────

SUPPORT_SOURCE_FACTORY: dict[str, Any] = {
    "ma60": lambda: MaSource(60),
    "ma20": lambda: MaSource(20),
    "ma120": lambda: MaSource(120),
    "ma240": lambda: MaSource(240),
    # 老命名（下划线）兼容
    "ma_60": lambda: MaSource(60),
    "ma_20": lambda: MaSource(20),
    "ma_120": lambda: MaSource(120),
    "ma_250": lambda: MaSource(240),
    "low_3m": lambda: RollingLowSource(63),
    "year_low": lambda: RollingLowSource(None),
}


def build_sources(support_sources: list[str]) -> tuple[list[Any], Optional[float]]:
    """把配置的支撑源列表装配为来源策略列表。

    返回值: (sources, dividend_anchor)
      - "dividend_anchor" 特殊处理：不进强/弱候选，仅作 extreme（见骨架）；
      - 其余：若命中 SUPPORT_SOURCE_FACTORY 走兼容映射，否则按指标表达式处理。

    Args:
        support_sources: 老写法如 ["dividend_anchor","ma60","low_3m","year_low"]，
                         新写法如 ["MA60", "MIN(MA20,MA240)"]。
    """
    sources: list[Any] = []
    anchor: Optional[float] = None
    for name in support_sources:
        key = str(name).strip()
        if key == "dividend_anchor":
            anchor = 0.0  # 占位：extreme 由调用方传入 dividend_anchor 实际值
            continue
        if key in SUPPORT_SOURCE_FACTORY:
            sources.append(SUPPORT_SOURCE_FACTORY[key]())
        else:
            # 指标表达式来源（新写法）
            sources.append(IndicatorExprSource(key))
    return sources, anchor


class RowContext:
    """轻量上下文：仅基于一行 K 线，供只有 row 的调用方（如 MultiBuyStrategy）。

    MaSource/RollingLowSource 会优先读 row 的预计算列（ma60/low_3m/year_low），
    基本不依赖本上下文。IndicatorExprSource 需要表达式求值，本类尽力从 row 列取
    原始值（close/high/low）+ 基础退算，无法求复合表达式时返回 0。
    """

    def __init__(self, row: pd.Series):
        self.row = row

    def ma(self, window: int) -> float:
        key = f"ma{window}"
        if key in self.row.index and not pd.isna(self.row.get(key)):
            return float(self.row.get(key))
        return 0.0

    def rolling_low(self, window: Optional[int] = None) -> float:
        key = "low_3m" if window and window <= 63 else "year_low"
        if key in self.row.index and not pd.isna(self.row.get(key)):
            return float(self.row.get(key))
        v = self.row.get("low")
        return float(v) if v is not None and not pd.isna(v) and v > 0 else 0.0

    def eval(self, expr: str) -> float:
        # 尽力求值：仅支持「单个名称」引用 row 列；复合表达式返回 0（调用方降级）
        expr = (expr or "").strip()
        try:
            return float(expr)
        except (ValueError, TypeError):
            pass
        if expr in self.row.index:
            v = self.row.get(expr)
            return float(v) if v is not None and not pd.isna(v) and v > 0 else 0.0
        return 0.0
