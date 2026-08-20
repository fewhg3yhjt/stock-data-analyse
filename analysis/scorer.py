"""四维评分系统 — 对齐个股 v4.5 Part6

Prompt原文:
  维度与权重:
    盈利能力 30% | 估值水平 30% | 技术趋势 20% | 资金/情绪 20%

  评级标准:
    ≥7分 + 逻辑坚实 → ✅ 可操作
    5-7分 + 逻辑有瑕疵 → ⚠️ 低仓位参与
    <5分 或 逻辑不成立 → ❌ 不碰
"""

from typing import Optional

# 默认权重
DEFAULT_WEIGHTS = {
    "profitability": 0.30,
    "valuation": 0.30,
    "technical": 0.20,
    "sentiment": 0.20,
}

# 基金权重（v4.5-Fund）
FUND_WEIGHTS = {
    "quality": 0.25,
    "valuation": 0.30,
    "technical": 0.25,
    "sentiment": 0.20,
}


def compute_weighted_score(
    dimension_scores: dict[str, float],
    weights: Optional[dict[str, float]] = None,
) -> float:
    """计算加权总分

    Args:
        dimension_scores: {"profitability": 1-10, "valuation": 1-10, ...}
        weights: 各维度权重，默认=Prompt指定个股权重

    Returns:
        加权总分(0-10)
    """
    weights = weights or DEFAULT_WEIGHTS
    total = 0.0
    for dim, score in dimension_scores.items():
        w = weights.get(dim, 0)
        total += score * w
    return round(total, 1)


def determine_rating(
    total_score: float,
    logic_sound: bool,
) -> str:
    """根据总分和逻辑判断评级

    Returns:
        "可操作" / "低仓位参与" / "不碰"
    """
    if not logic_sound:
        return "不碰"
    if total_score >= 7:
        return "可操作"
    if total_score >= 5:
        return "低仓位参与"
    return "不碰"
