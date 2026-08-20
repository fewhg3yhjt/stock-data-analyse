"""盈利能力评级 — 对齐个股 v4.5 Part2

Prompt原文:
  ROE:           优秀>15%  | 及格8%-15%  | 不及格<8%
  净利润率:      优秀>10%  | 及格5%-10%  | 不及格<5%
  净现比:        优秀>1    | 及格0.8-1   | 不及格<0.8
  毛利率趋势:    优秀上升  | 及格稳定    | 不及格下降

  及格项: X/4项 → ✅ 通过(≥3项) / ❌ 不通过
"""

from typing import Optional, Literal

Rating = Literal["优秀", "及格", "不及格"]


def rate_roe(roe: float) -> tuple[Rating, str]:
    """ROE评级"""
    if roe >= 15:
        return ("优秀", f"{roe:.1f}% ≥ 15%")
    if roe >= 8:
        return ("及格", f"8% ≤ {roe:.1f}% < 15%")
    return ("不及格", f"{roe:.1f}% < 8%")


def rate_net_profit_margin(margin: float) -> tuple[Rating, str]:
    """净利润率评级"""
    if margin >= 10:
        return ("优秀", f"{margin:.1f}% ≥ 10%")
    if margin >= 5:
        return ("及格", f"5% ≤ {margin:.1f}% < 10%")
    return ("不及格", f"{margin:.1f}% < 5%")


def rate_cash_ratio(ratio: float) -> tuple[Rating, str]:
    """净现比评级"""
    if ratio >= 1.0:
        return ("优秀", f"{ratio:.2f} ≥ 1.0")
    if ratio >= 0.8:
        return ("及格", f"0.8 ≤ {ratio:.2f} < 1.0")
    return ("不及格", f"{ratio:.2f} < 0.8")


def rate_gross_margin_trend(trend: str) -> tuple[Rating, str]:
    """毛利率趋势评级"""
    trend_map = {
        "上升": ("优秀", "毛利率持续上升"),
        "稳定": ("及格", "毛利率基本稳定"),
        "下降": ("不及格", "毛利率持续下降"),
    }
    return trend_map.get(trend, ("不及格", "数据不足"))


def rate_all(
    roe: float,
    net_profit_margin: float,
    cash_ratio: float,
    gross_margin_trend: str,
) -> dict:
    """四项指标全部评级

    Returns:
        {"ratings": {指标: 评级}, "passed": int, "conclusion": str}
    """
    results = {
        "ROE": rate_roe(roe),
        "净利润率": rate_net_profit_margin(net_profit_margin),
        "净现比": rate_cash_ratio(cash_ratio),
        "毛利率趋势": rate_gross_margin_trend(gross_margin_trend),
    }
    ratings = {k: v[0] for k, v in results.items()}
    details = {k: v[1] for k, v in results.items()}

    passed = sum(1 for r in ratings.values() if r in ("优秀", "及格"))
    conclusion = "✅ 通过" if passed >= 3 else "❌ 不通过"

    return {
        "ratings": ratings,
        "details": details,
        "passed": passed,
        "conclusion": conclusion,
    }
