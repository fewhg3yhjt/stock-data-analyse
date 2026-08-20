"""V6.0 卖出决策树增强 — 逻辑止损 > 价格止损 > 时间止损 > 三层止盈

对齐《四维一体实战投资体系 V6.0》第二编第六步（§6.2 与 §10 速查表）：

  优先级: 逻辑止损(最高) > 价格止损(贝塔保护) > 时间止损 > 三层止盈

  ① 逻辑止损: 建仓核心假设(thesis)被证伪（分红断崖/增速拐点/价格破位）→ 清仓。
     纯回测无法推出 → 输入 thesis_ok 布尔；None=未核对 → 不触发（人工核对信号）。
  ② 价格止损 + 贝塔保护: 仅 价值(B/E)/金融(D) 设硬止损(均价×-15%)；
     周期(C)/成长(A) 不设硬止损只看逻辑。
     个股跌15% 且 同期沪深300跌<5% → 硬止损；沪深300跌>10% → 暂停转逻辑审视；
     股债收益差>5.5% → 取消硬止损改时间止损。
  ③ 时间止损: 强周期(C) 6个月无右侧信号→减半；成长(A) 6个月无营收加速→减半；
     赦免条款2个月(grace_days=60)。
  ④ 三层止盈: 左侧比例按类型 A 20% / B·D 40% / C 60%（各档按前高90-95→95-100 分步）；
     右侧回撤 A·C 5% / B·D 3% 清仓；
     止盈硬上限 MIN(近12M最高×1.05, MA250×1.2)。

纯函数无副作用，输入均为已算好的派生值，表驱动可测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 左侧止盈比例按类型（总仓位比例，分两档各半）
LEFT_SIDE_RATIO_BY_TYPE = {
    "A": 0.20,
    "B": 0.40,
    "C": 0.60,
    "D": 0.40,
    "E": 0.40,
}
# 右侧移动止盈回撤阈值按类型
RIGHT_DD_BY_TYPE = {
    "A": 0.05,
    "B": 0.03,
    "C": 0.05,
    "D": 0.03,
    "E": 0.03,
}
# 硬止损扣减率（仅价值/金融）: 止损价 = 均价 × (1 - 扣减率)
HARD_STOP_BY_TYPE = {
    "B": 0.15,
    "E": 0.15,
    "D": 0.10,
    "A": None,   # 成长：不设硬止损
    "C": None,   # 周期：不设硬止损
}
# 时间止损参数
TIME_STOP_HOLDING_DAYS = 180   # 6个月
GRACE_DAYS = 60                # 赦免2个月
# 贝塔保护阈值
BETA_CUT_MARKET_DROP = 0.05    # 沪深300跌<5% → 硬止损成立
BETA_SUSPEND_MARKET_DROP = 0.10  # 沪深300跌>10% → 暂停硬止损
ERP_HARD_STOP_WAIVER = 0.055   # 股债收益差>5.5% → 取消硬止损
# 左侧止盈区间（前高比例）
LEFT_ZONE_WARN = 0.90
LEFT_ZONE_TP1 = 0.95


@dataclass
class SellDecision:
    priority: int            # 1=逻辑 2=价格 3=时间 4=止盈 0=持有
    action: str              # clear / partial_sell / hold
    rule: str
    reason: str
    ratio: float = 0.0       # 卖出仓位比例（partial_sell 时有效）
    notes: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "priority": self.priority,
            "action": self.action,
            "rule": self.rule,
            "reason": self.reason,
            "ratio": self.ratio,
            "notes": self.notes,
            "detail": self.detail,
        }


# ═══════════════════════════════════════════════════════════
# ① 逻辑止损（最高优先）
# ═══════════════════════════════════════════════════════════

def judge_logic_stop(*, thesis_ok: Optional[bool]) -> Optional[SellDecision]:
    """逻辑止损：核心假设证伪 → 清仓。

    thesis_ok:
      False → 证伪，清仓
      True  → 假设仍成立，不触发
      None  → 未核对（回测中人工/LLM 信号），不触发并标注
    """
    if thesis_ok is False:
        return SellDecision(
            priority=1, action="clear", rule="逻辑止损",
            reason="核心假设被证伪（分红断崖/增速拐点/价格破位），最高优先级清仓",
        )
    return None


# ═══════════════════════════════════════════════════════════
# ② 价格止损 + 贝塔保护
# ═══════════════════════════════════════════════════════════

def price_stop_line(
    *, avg_cost: float,
    left_tp_triggered: bool = False,
    base_discount: Optional[float] = None,
) -> float:
    """止损线（含保本线上移）。

    左侧止盈已触发 → 止损线上移至持仓成本价；否则 均价×(1-扣减率)。
    """
    if avg_cost <= 0:
        return 0.0
    if left_tp_triggered:
        return avg_cost
    discount = base_discount if base_discount is not None else 0.15
    return avg_cost * (1 - discount)


def judge_price_stop(
    *,
    stock_type: str,
    low: float,
    avg_cost: float,
    csi300_drop_pct: Optional[float] = None,   # 同期沪深300跌幅（小数，负=跌）
    erp: Optional[float] = None,               # 股债收益差（小数）
    left_tp_triggered: bool = False,
) -> Optional[SellDecision]:
    """价格止损 + 贝塔保护（仅价值B/E、金融D 设硬止损）。

    返回 None 表示未触发或已暂停（暂停时 notes 记录，交时间止损/止盈继续判定）。
    贝塔保护:
      个股破止损线 且 沪深300跌<5%        → 硬止损清仓
      个股破止损线 且 沪深300跌>10%       → 暂停硬止损，转逻辑审视（hold）
      个股破止损线 且 股债收益差>5.5%     → 取消硬止损，改时间止损（hold）
      个股破止损线 且 沪深300数据缺失     → 硬止损照常（宏观缺失不误杀不清，宁可执行止损）
    """
    t = (stock_type or "B").upper()
    discount = HARD_STOP_BY_TYPE.get(t)
    if discount is None:
        return None   # 成长/周期：不设硬止损，只看逻辑
    if avg_cost <= 0 or low <= 0:
        return None

    stop_line = price_stop_line(avg_cost=avg_cost, left_tp_triggered=left_tp_triggered,
                                base_discount=discount)
    if low > stop_line:
        return None

    # 跌破止损线 → 贝塔保护判定
    drop_pct = abs(csi300_drop_pct) if csi300_drop_pct is not None and csi300_drop_pct < 0 else 0.0

    # 股债收益差>5.5% → 取消硬止损，改时间止损
    if erp is not None and erp > ERP_HARD_STOP_WAIVER:
        return SellDecision(
            priority=2, action="hold", rule="贝塔保护(取消硬止损)",
            reason=f"股债收益差{erp:.1%}>5.5% 极度低估：取消硬止损，改时间止损",
            detail={"stop_line": round(stop_line, 2), "low": round(low, 2)},
        )

    # 沪深300跌>10% → 暂停硬止损，转逻辑审视
    if csi300_drop_pct is not None and csi300_drop_pct <= -BETA_SUSPEND_MARKET_DROP:
        return SellDecision(
            priority=2, action="hold", rule="贝塔保护(暂停硬止损)",
            reason=f"同期沪深300跌{abs(csi300_drop_pct):.1%}>10%（系统性下跌）：暂停硬止损，转逻辑审视",
            detail={"stop_line": round(stop_line, 2), "low": round(low, 2),
                    "csi300_drop_pct": round(csi300_drop_pct, 4)},
        )

    # 沪深300跌<5%（或数据缺失）→ 个股相对弱势，硬止损成立
    if csi300_drop_pct is None or csi300_drop_pct > -BETA_CUT_MARKET_DROP:
        return SellDecision(
            priority=2, action="clear", rule="价格止损",
            reason=(f"最低价{low:.2f}≤止损线{stop_line:.2f}"
                    f"（成本{avg_cost:.2f}×{1-discount:.0%}）"
                    + (f"，同期沪深300仅跌{abs(csi300_drop_pct):.1%}<5%，个股相对弱势"
                       if csi300_drop_pct is not None else "，无宏观对冲信号")),
            detail={"stop_line": round(stop_line, 2), "low": round(low, 2)},
        )

    # 5%~10% 中性区间 → 硬止损照常（个股跌15%仍属相对弱势）
    return SellDecision(
        priority=2, action="clear", rule="价格止损",
        reason=f"最低价{low:.2f}≤止损线{stop_line:.2f}（成本{avg_cost:.2f}×{1-discount:.0%}）",
        detail={"stop_line": round(stop_line, 2), "low": round(low, 2)},
    )


# ═══════════════════════════════════════════════════════════
# ③ 时间止损
# ═══════════════════════════════════════════════════════════

def judge_time_stop(
    *,
    stock_type: str,
    holding_days: int,
    right_signal_occurred: Optional[bool] = None,   # 是否出现右侧信号
    revenue_accel: Optional[bool] = None,           # 营收是否加速
    holding_days_limit: int = TIME_STOP_HOLDING_DAYS,
    grace_days: int = GRACE_DAYS,
) -> Optional[SellDecision]:
    """时间止损：仅 强周期(C) 无右侧信号 / 成长(A) 无营收加速。

    持仓超过 6个月(180天) + 赦免期(60天) 仍无信号 → 减半。
    信号数据缺失(None) → 视为未出现（从严，但标注）。
    """
    t = (stock_type or "B").upper()
    if t not in ("A", "C"):
        return None

    limit = holding_days_limit + grace_days
    if holding_days <= limit:
        return None

    if t == "C":
        signal_ok = right_signal_occurred
        label = "右侧信号"
    else:  # A
        signal_ok = revenue_accel
        label = "营收加速"

    if signal_ok is True:
        return None   # 信号出现，赦免

    return SellDecision(
        priority=3, action="partial_sell", rule="时间止损",
        ratio=0.5,
        reason=f"{'强周期' if t=='C' else '成长'}持仓{holding_days}天>{limit}天仍无{label}"
               f"（{'数据缺失按未出现计' if signal_ok is None else '未出现'}）→ 减半",
        detail={"holding_days": holding_days, "grace_days": grace_days,
                "signal": signal_ok},
    )


# ═══════════════════════════════════════════════════════════
# ④ 三层止盈
# ═══════════════════════════════════════════════════════════

def take_profit_hard_cap(year_high: Optional[float], ma250: Optional[float]) -> Optional[float]:
    """止盈硬上限 = MIN(近12M最高×1.05, MA250×1.2)。"""
    if year_high is None or year_high <= 0:
        return None
    candidates = [year_high * 1.05]
    if ma250 is not None and ma250 > 0:
        candidates.append(ma250 * 1.2)
    return min(candidates)


def judge_left_side(
    *,
    stock_type: str,
    high: float,
    year_high: float,
    left_tiers_sold: int,
    left_ratio_by_type: Optional[dict] = None,
) -> Optional[SellDecision]:
    """左侧止盈：前高 90-95%→预警档、95-100%→第一档，各卖 left_ratio/2。"""
    ratio_map = left_ratio_by_type or LEFT_SIDE_RATIO_BY_TYPE
    t = (stock_type or "B").upper()
    if year_high <= 0 or high <= 0:
        return None
    pct = high / year_high
    total_ratio = ratio_map.get(t, 0.40)
    step = total_ratio / 2

    if pct >= 1.00:
        return None   # 突破前高 → 转右侧移动止盈，左侧档不再卖出

    if pct >= LEFT_ZONE_TP1 and left_tiers_sold < 2:
        return SellDecision(
            priority=4, action="partial_sell", rule="左侧止盈(第一档)",
            ratio=round(step, 3),
            reason=f"最高价{high:.2f}达前高{year_high:.2f}的{pct:.1%}（95-100%区间），"
                   f"卖出{step:.1%}仓位",
            detail={"pct_of_year_high": round(pct, 4), "left_tiers_sold": left_tiers_sold},
        )
    if pct >= LEFT_ZONE_WARN and left_tiers_sold < 1:
        return SellDecision(
            priority=4, action="partial_sell", rule="左侧止盈(预警档)",
            ratio=round(step, 3),
            reason=f"最高价{high:.2f}达前高{year_high:.2f}的{pct:.1%}（90-95%区间），"
                   f"卖出{step:.1%}仓位",
            detail={"pct_of_year_high": round(pct, 4), "left_tiers_sold": left_tiers_sold},
        )
    return None


def judge_right_side(
    *,
    stock_type: str,
    right_peak: float,
    close: float,
    right_dd_by_type: Optional[dict] = None,
) -> Optional[SellDecision]:
    """右侧移动止盈：突破后从峰值回撤超阈值 → 清仓。"""
    dd_map = right_dd_by_type or RIGHT_DD_BY_TYPE
    t = (stock_type or "B").upper()
    if right_peak <= 0 or close <= 0:
        return None
    dd = (right_peak - close) / right_peak
    threshold = dd_map.get(t, 0.05)
    if dd >= threshold:
        return SellDecision(
            priority=4, action="clear", rule="右侧止盈(移动清仓)",
            reason=f"从峰值{right_peak:.2f}回撤{dd:.1%}（阈值{t}类{threshold:.0%}）",
            detail={"right_peak": round(right_peak, 2), "close": round(close, 2),
                    "drawdown": round(dd, 4), "threshold": threshold},
        )
    return None


# ═══════════════════════════════════════════════════════════
# 综合卖出决策树
# ═══════════════════════════════════════════════════════════

def judge_sell_tree_v6(
    *,
    stock_type: str,
    # 逻辑止损
    thesis_ok: Optional[bool] = None,
    # 价格止损
    low: float = 0.0,
    avg_cost: float = 0.0,
    csi300_drop_pct: Optional[float] = None,
    erp: Optional[float] = None,
    left_tp_triggered: bool = False,
    # 时间止损
    holding_days: int = 0,
    right_signal_occurred: Optional[bool] = None,
    revenue_accel: Optional[bool] = None,
    # 三层止盈
    high: float = 0.0,
    year_high: float = 0.0,
    ma250: Optional[float] = None,
    right_peak: float = 0.0,
    close: float = 0.0,
    left_tiers_sold: int = 0,
    left_ratio_by_type: Optional[dict] = None,
    right_dd_by_type: Optional[dict] = None,
) -> SellDecision:
    """V6.0 卖出决策树综合判定（纯函数）。

    优先级: 逻辑止损 > 价格止损 > 时间止损 > 三层止盈。
    价格止损的「贝塔保护暂停」为 hold，不终止，继续检查时间止损/止盈。
    """
    notes: list[str] = []

    # ── P1 逻辑止损 ──
    d = judge_logic_stop(thesis_ok=thesis_ok)
    if d:
        return d

    # ── P2 价格止损（含贝塔保护）──
    d = judge_price_stop(
        stock_type=stock_type, low=low, avg_cost=avg_cost,
        csi300_drop_pct=csi300_drop_pct, erp=erp,
        left_tp_triggered=left_tp_triggered,
    )
    if d and d.action == "clear":
        return d
    if d and d.action == "hold":
        notes.append(d.rule)

    # ── P3 时间止损 ──
    d = judge_time_stop(
        stock_type=stock_type, holding_days=holding_days,
        right_signal_occurred=right_signal_occurred,
        revenue_accel=revenue_accel,
    )
    if d:
        return d

    # ── P4 三层止盈 ──
    left = judge_left_side(
        stock_type=stock_type, high=high, year_high=year_high,
        left_tiers_sold=left_tiers_sold, left_ratio_by_type=left_ratio_by_type,
    )
    if left:
        return left
    right = judge_right_side(
        stock_type=stock_type, right_peak=right_peak, close=close,
        right_dd_by_type=right_dd_by_type,
    )
    if right:
        return right

    return SellDecision(
        priority=0, action="hold", rule="继续持有",
        reason="各止损/止盈规则均未触发",
        notes=notes,
    )
