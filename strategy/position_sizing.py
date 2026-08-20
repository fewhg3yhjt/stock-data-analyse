"""V6.0 仓位管理 — 类型上限 × 市场乘数 × 乖离率乘数 + 流动性核查 + 行业集中度

对齐《四维一体实战投资体系 V6.0》第二编第四步：
  单票仓位 = 类型上限 × 市场状态乘数 × 乖离率乘数
  类型上限表: 稳定价值15%(极端低估+高股息→20%) / 强周期12%(强右侧→15%) /
              科技成长10%(扭亏双击→15%) / 金融杠杆12%(PB极低+高股息→15%) /
              混合取低 / 纸面富贵型 ×50%
  流动性核查: 近20日日均成交额 vs 买入金额:
              >10倍 → 标准 / 5~10倍 → ×80% / <5倍 → 暂缓
  行业集中度: 单行业≤30%、前三行业≤60%（组合层接口，本期预留）

纯函数无副作用，输入均为已算好的派生值，表驱动可测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 类型上限表（占总账户 %）
TYPE_CAP = {
    "A": 0.10,   # 科技成长
    "B": 0.15,   # 稳定价值（价值白马）
    "C": 0.12,   # 强周期
    "D": 0.12,   # 金融杠杆
    "E": 0.15,   # 深度价值（第一类子类，同价值）
}
# 特殊条款上调后的上限
TYPE_CAP_UPGRADE = {
    "A": 0.15,   # 扭亏双击
    "B": 0.20,   # 极端低估 + 高股息
    "C": 0.15,   # 强右侧
    "D": 0.15,   # PB极低 + 高股息
    "E": 0.20,   # 深度价值（本质即 极端低估+高股息）
}
# 纸面富贵型（应收/营收>80%）仓位减半
PAPER_RICH_FACTOR = 0.5

# 乖离率乘数（§10 速查表）
#   >+20% 超买降级（已由市场状态处理，此处 +20% 兜底0.7）
#   +10~20% → 0.7 / ±10% → 1.0 / -10~-20% → 1.0 / <-20% → 1.2
BIAS_OVERBOUGHT = 20.0
BIAS_HIGH = 10.0
BIAS_DEEP_OVERSOLD = -20.0
BIAS_OVERSOLD = -10.0


def type_cap(stock_type: str, *, upgrade: bool = False) -> float:
    """类型上限（小数）。

    Parameters
    ----------
    stock_type : A/B/C/D/E
    upgrade : 是否触发特殊条款上调（扭亏双击/极端低估+高股息/强右侧/PB极低+高股息）
    """
    t = (stock_type or "B").upper()
    if upgrade:
        return TYPE_CAP_UPGRADE.get(t, TYPE_CAP.get(t, 0.15))
    return TYPE_CAP.get(t, 0.15)


def bias_multiplier(bias_ratio: Optional[float]) -> float:
    """乖离率乘数（对齐 §10 速查表）。

    >+20% → 0.7（超买降级，市场状态通常已降级，此处兜底）
    +10~20% → 0.7
    ±10% 以内 → 1.0
    -10~-20% → 1.0
    <-20% → 1.2（深度超卖，加仓）
    None/数据不足 → 1.0（不惩罚）
    """
    if bias_ratio is None:
        return 1.0
    if bias_ratio >= BIAS_HIGH:         # ≥ +10%（含超买）→ 0.7
        return 0.7
    if bias_ratio >= BIAS_OVERSOLD:     # [-10, +10] → 1.0
        return 1.0
    if bias_ratio >= BIAS_DEEP_OVERSOLD:  # [-20, -10) → 1.0
        return 1.0
    return 1.2                          # < -20% 深度超卖


def compute_position_pct(
    *,
    stock_type: str = "B",
    market_multiplier: float = 1.0,
    bias_ratio: Optional[float] = None,
    paper_rich: bool = False,
    upgrade: bool = False,
) -> float:
    """单票仓位上限（占总账户 %）。

    仓位 = 类型上限 × 市场乘数 × 乖离率乘数；纸面富贵型整体 ×50%。
    最终钳制不超过类型上限（「上限」概念，不允许超限）。
    """
    cap = type_cap(stock_type, upgrade=upgrade)
    if paper_rich:
        cap *= PAPER_RICH_FACTOR
    raw = type_cap(stock_type, upgrade=upgrade) * market_multiplier * bias_multiplier(bias_ratio)
    if paper_rich:
        raw *= PAPER_RICH_FACTOR
    return round(min(raw, cap) * 100, 1)


@dataclass
class LiquidityResult:
    multiplier: float
    status: str               # 标准 / 缩减80% / 暂缓
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"multiplier": self.multiplier, "status": self.status, "detail": self.detail}


def liquidity_check(
    *,
    daily_avg_amount: Optional[float],   # 近20日日均成交额（元）
    buy_amount: Optional[float],         # 计划买入金额（元）
) -> LiquidityResult:
    """流动性核查：日均成交额 ≥ 买入金额 × 倍数。

    >10倍 → 标准 / 5~10倍 → ×80% / <5倍 → 暂缓。
    数据不足（任一缺失）→ 标准（不误杀，标注待人工核对）。
    """
    if daily_avg_amount is None or buy_amount is None or daily_avg_amount <= 0 or buy_amount <= 0:
        return LiquidityResult(1.0, "数据不足", {"ratio": None})
    ratio = daily_avg_amount / buy_amount
    if ratio > 10:
        return LiquidityResult(1.0, "标准", {"ratio": round(ratio, 1), "note": "日均成交额 > 买入金额×10倍"})
    if ratio >= 5:
        return LiquidityResult(0.8, "缩减80%", {"ratio": round(ratio, 1), "note": "5~10倍，仓位×80%"})
    return LiquidityResult(0.0, "暂缓", {"ratio": round(ratio, 1), "note": "<5倍，暂缓买入"})


def sector_concentration(
    *,
    sector: Optional[str] = None,
    sector_weights: Optional[dict] = None,   # {行业: 占总账户%}
    max_single: float = 0.30,
    max_top3: float = 0.60,
) -> dict:
    """行业集中度核查（组合层接口，本期预留）。

    单行业≤30%、前三行业合计≤60%。未提供持仓分布 → 返回 "未核查"。
    """
    if not sector or not sector_weights:
        return {"checked": False, "status": "未核查", "detail": "组合层接口预留，需组合持仓输入"}
    current = sector_weights.get(sector, 0.0)
    single_ok = current <= max_single
    top3 = sum(sorted(sector_weights.values(), reverse=True)[:3])
    top3_ok = top3 <= max_top3
    return {
        "checked": True,
        "single_ok": single_ok,
        "top3_ok": top3_ok,
        "single_pct": round(current * 100, 1),
        "top3_pct": round(top3 * 100, 1),
        "status": "通过" if (single_ok and top3_ok) else "超限",
    }
