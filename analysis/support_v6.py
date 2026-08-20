"""V6.0 支撑位判定器 — 三重锚→支撑位交叉验证（第二编 §4.2）

对齐《四维一体实战投资体系 V6.0》：

  综合弱支撑（阵地）= 锚定价②、MA60当前值、近期低点、52周最低 → 取次低值
      再按 MA60 方向修正（仅阵地）:
        向上（今日 > 5日前）→ 支撑增强，阵地上修 +3%
        走平（今日在5日前 ±0.5% 内）→ 维持原值
        向下（今日 < 5日前）→ 支撑减弱，阵地下修 -3%
  综合强支撑（铁底）= 锚定价③、MA60下沿×0.95、52周最低 → 取最低值
      不做方向修正。

三重锚（供上层算锚②/锚③，V6.0 口径与 ValuationHelper.triple_anchor 一致）:
  锚定价② = 每股分红 ÷ 3.4%  （安全边际线）
  锚定价③ = 每股分红 ÷ 4.0%  （极端低估线）

纯函数无副作用，输入均为已算好的候选值，表驱动可测。
"""

from dataclasses import dataclass, field
from typing import Optional

# MA60 方向修正系数
ADJ_UP = 1.03     # 向上 → 阵地上修 +3%
ADJ_FLAT = 1.00   # 走平 → 维持
ADJ_DOWN = 0.97   # 向下 → 阵地下修 -3%
# 走平判定窗口: 今日 MA60 在 5 日前 ±0.5% 范围内
FLAT_TOLERANCE = 0.005
# 铁底 MA60 下沿折扣
MA60_LOWER_DISCOUNT = 0.95


@dataclass
class SupportV6Result:
    """V6.0 支撑位判定结果"""
    zhen_di: float              # 阵地（综合弱支撑，MA60方向修正后）
    tie_di: float               # 铁底（综合强支撑，最低值）
    ma60_direction: str         # 向上 / 走平 / 向下 / 数据不足
    adjust_factor: float        # 阵地修正系数（1.03 / 1.00 / 0.97）
    weak_raw: float             # 修正前弱支撑（次低值）
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "zhen_di": self.zhen_di,
            "tie_di": self.tie_di,
            "ma60_direction": self.ma60_direction,
            "adjust_factor": self.adjust_factor,
            "weak_raw": self.weak_raw,
            "detail": self.detail,
        }


def _lowest(values: list[Optional[float]]) -> float:
    """取有效值中的最低值；无有效值返回 0"""
    vals = [v for v in values if v is not None and v > 0]
    return min(vals) if vals else 0.0


def _second_lowest(values: list[Optional[float]]) -> float:
    """取有效值中的次低值（升序第2个）；不足2个则取最低值；无有效值返回 0"""
    vals = sorted(v for v in values if v is not None and v > 0)
    if not vals:
        return 0.0
    return vals[1] if len(vals) > 1 else vals[0]


def judge_ma60_direction(ma60: Optional[float],
                         ma60_5d_ago: Optional[float]) -> tuple[str, float]:
    """MA60 方向判定（对齐 §4.2 表）。

    Returns: (direction, adjust_factor)
      向上: 今日 > 5日前 → "向上", 1.03
      走平: |今日−5日前|/5日前 ≤ 0.5% → "走平", 1.00
      向下: 今日 < 5日前 → "向下", 0.97
      数据不足: 任一缺失 → "数据不足", 1.00
    """
    if ma60 is None or ma60_5d_ago is None or ma60_5d_ago <= 0 or ma60 <= 0:
        return "数据不足", ADJ_FLAT
    delta = (ma60 - ma60_5d_ago) / ma60_5d_ago
    if abs(delta) <= FLAT_TOLERANCE:
        return "走平", ADJ_FLAT
    if delta > 0:
        return "向上", ADJ_UP
    return "向下", ADJ_DOWN


def calc_support_v6(
    *,
    anchor2: Optional[float] = None,      # 锚定价②（每股分红÷3.4%）
    anchor3: Optional[float] = None,      # 锚定价③（每股分红÷4.0%）
    ma60: Optional[float] = None,         # MA60 当前值
    ma60_5d_ago: Optional[float] = None,  # 5个交易日前 MA60（方向判定用）
    low_3m: Optional[float] = None,       # 近期低点（近3月低点）
    low_52w: Optional[float] = None,      # 52周最低价
) -> SupportV6Result:
    """V6.0 支撑位交叉验证（纯函数）。

    阵地 = 次低值(锚②, MA60, 近3月低点, 52周低) × MA60方向修正
    铁底 = 最低值(锚③, MA60×0.95, 52周低)

    Parameters
    ----------
    anchor2/anchor3 : 三重锚②③，由上层从分红数据算（ValuationHelper.triple_anchor 或直接 ÷3.4%/4.0%）
    ma60 / ma60_5d_ago : MA60 当前值与5交易日前值，判定方向与±3%修正
    low_3m / low_52w : 近期低点 / 52周最低价
    """
    # ── MA60 方向 → 阵地修正系数 ──
    direction, factor = judge_ma60_direction(ma60, ma60_5d_ago)

    # ── 阵地: 次低值(锚②, MA60, 近3月低, 52周低) × 修正系数 ──
    weak_raw = _second_lowest([anchor2, ma60, low_3m, low_52w])
    zhen_di = round(weak_raw * factor, 2) if weak_raw > 0 else 0.0

    # ── 铁底: 最低值(锚③, MA60下沿×0.95, 52周低)，不做方向修正 ──
    ma60_lower = (ma60 * MA60_LOWER_DISCOUNT) if ma60 is not None and ma60 > 0 else None
    tie_di = _lowest([anchor3, ma60_lower, low_52w])

    return SupportV6Result(
        zhen_di=round(zhen_di, 2),
        tie_di=round(tie_di, 2),
        ma60_direction=direction,
        adjust_factor=factor,
        weak_raw=round(weak_raw, 2),
        detail={
            "candidates_weak": {
                "anchor2": anchor2, "ma60": ma60, "low_3m": low_3m, "low_52w": low_52w,
            },
            "candidates_tie": {
                "anchor3": anchor3, "ma60_lower": ma60_lower, "low_52w": low_52w,
            },
            "weak_method": "次低值（升序第2个）",
            "tie_method": "最低值",
        },
    )


def anchor_prices(div_per_share: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """由每股分红直接算锚②/锚③（V6.0 口径）。

    Returns: (anchor2, anchor3)
      anchor2 = div ÷ 3.4%
      anchor3 = div ÷ 4.0%
    """
    if div_per_share is None or div_per_share <= 0:
        return None, None
    return round(div_per_share / 0.034, 2), round(div_per_share / 0.040, 2)
