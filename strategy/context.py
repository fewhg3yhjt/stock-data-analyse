"""规则执行统一上下文（FR-1.1）

设计意图（HLD §3.1）：
  - executor 签名统一为 `fn(ctx: RuleContext, params: dict) -> RuleResult`，
    而不是各自位置参数 —— 因为不同规则入参差异巨大；
  - 参数差异用「统一上下文字段（超集）」+「params（透传）」消化，签名保持稳定；
  - 上下文只放「规则可能用到的输入」，不放行为，随需扩展，不改 executor 签名。

关联模块：
  - `strategy/rule_registry.py`：RuleRegistry 按 type 派发 executor；
  - `indicators/context.py`：IndicatorContext 统一指标求值入口；
  - `strategy/support.py`：支撑位骨架与来源策略（也基于 IndicatorContext）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd


@dataclass
class RuleContext:
    """规则执行时的输入超集。

    每个字段都是「可选」的：executor 只读取自己关心的字段，
    未实例化的字段保持默认值。新增字段不影响既有 executor 签名。
    """

    row: Optional[pd.Series] = None            # 当前 K 线行
    df: Optional[pd.DataFrame] = None          # 整段 K 线（市场状态/趋势等需要）
    indicators: Any = None                     # IndicatorContext，指标求值入口
    current_price: float = 0.0
    avg_cost: float = 0.0
    total_cost: float = 0.0
    shares: float = 0.0
    position_phase: str = ""
    peak_price: float = 0.0
    year_high: float = 0.0
    market_state: str = ""
    dividend_anchor: Optional[float] = None

    # 衍生/过程值（executor 输出过程说明时可用）
    left_tier_sold: int = 0
    buy_stage: int = 0
    stop_loss_rate: float = 0.15
    drawdown_stop: float = 0.08
    min_profit_for_dd: float = 0.06
    previous_vol_ma5: float = 0.0

    # 富扩展：供 executor 按需放置任意派生输入
    extra: dict = field(default_factory=dict)

    # ── 便捷访问 ────────────────────────────────────────
    def get(self, key: str, default: Any = None) -> Any:
        """按名称取一个值（先查字段，再查 extra）。"""
        if hasattr(self, key) and getattr(self, key) not in (None, "", 0.0):
            return getattr(self, key)
        return self.extra.get(key, default)


@dataclass
class RuleResult:
    """规则执行结果。

    不同规则返回不同语义，这里用「动作 + 说明」的统一外壳承载，
    具体含义由 kind/type 决定（buy 规则返回批次计划，sell 规则返回卖点动作）。
    """

    triggered: bool = False
    action: str = "hold"             # hold / clear / partial_sell / buy_more / transition
    reason: str = ""
    detail: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "triggered": self.triggered,
            "action": self.action,
            "reason": self.reason,
            "detail": self.detail,
            "trades": self.trades,
        }
