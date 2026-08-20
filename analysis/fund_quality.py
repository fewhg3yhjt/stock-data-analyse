"""基金质量评估 — 对齐基金 v4.5-Fund Part2

Prompt原文（指数基金）:
  年化跟踪误差: <0.1%优/0.1%-0.3%及/>0.3%不及
  综合费率:     <0.2%/年优/0.2%-0.5%/年及/>0.5%/年不及
  基金股息率:   >2.5%优/1.5%-2.5%及/<1.5%不及
  净值偏离度:   日偏离<0.1%优/基本吻合及/明显偏离不及
  ≥3项及格 → ✅ 通过

Prompt原文（主动基金）:
  超额收益(近3年): >5%优/2%-5%及/<2%不及
  综合费率:        <1.0%/年优/1.0%-1.5%/年及/>1.5%/年不及
  夏普比率(近3年): >1优/0.5-1及/<0.5不及
  最大回撤(近3年): 优于同类平均/接近/劣于
  ≥3项及格 → ✅ 通过
"""

from typing import Literal, Optional

Rating = Literal["优秀", "及格", "不及格"]


def rate_tracking_error(error_pct: float) -> tuple[Rating, str]:
    """年化跟踪误差评级"""
    if error_pct < 0.1:
        return ("优秀", f"{error_pct}% < 0.1%")
    if error_pct <= 0.3:
        return ("及格", f"0.1% ≤ {error_pct}% ≤ 0.3%")
    return ("不及格", f"{error_pct}% > 0.3%")


def rate_index_fund_fee(fee_pct: float) -> tuple[Rating, str]:
    """指数基金综合费率评级（管理+托管）"""
    if fee_pct < 0.2:
        return ("优秀", f"{fee_pct}%/年 < 0.2%")
    if fee_pct <= 0.5:
        return ("及格", f"0.2% ≤ {fee_pct}%/年 ≤ 0.5%")
    return ("不及格", f"{fee_pct}%/年 > 0.5%")


def rate_fund_dividend_yield(yield_pct: float) -> tuple[Rating, str]:
    """基金股息率评级"""
    if yield_pct > 2.5:
        return ("优秀", f"{yield_pct}% > 2.5%")
    if yield_pct >= 1.5:
        return ("及格", f"1.5% ≤ {yield_pct}% ≤ 2.5%")
    return ("不及格", f"{yield_pct}% < 1.5%")


def rate_nav_deviation(deviation_pct: float) -> tuple[Rating, str]:
    """净值偏离度评级"""
    if deviation_pct < 0.1:
        return ("优秀", f"日偏离{deviation_pct}% < 0.1%")
    if deviation_pct < 0.5:
        return ("及格", f"基本吻合（日偏离{deviation_pct}%）")
    return ("不及格", f"明显偏离（日偏离{deviation_pct}%）")


def rate_index_fund_all(
    tracking_error: float,
    fee_pct: float,
    div_yield: float,
    nav_deviation: float,
) -> dict:
    """指数基金全部4项质量评级"""
    results = {
        "年化跟踪误差": rate_tracking_error(tracking_error),
        "综合费率": rate_index_fund_fee(fee_pct),
        "基金股息率": rate_fund_dividend_yield(div_yield),
        "净值偏离度": rate_nav_deviation(nav_deviation),
    }
    ratings = {k: v[0] for k, v in results.items()}
    passed = sum(1 for r in ratings.values() if r in ("优秀", "及格"))
    return {
        "ratings": ratings,
        "passed": passed,
        "conclusion": "✅ 通过" if passed >= 3 else "❌ 不通过",
    }


def rate_active_excess_return(ret_pct: float) -> tuple[Rating, str]:
    """主动基金超额收益评级"""
    if ret_pct > 5:
        return ("优秀", f"{ret_pct}% > 5%")
    if ret_pct >= 2:
        return ("及格", f"2% ≤ {ret_pct}% ≤ 5%")
    return ("不及格", f"{ret_pct}% < 2%")


def rate_active_fund_fee(fee_pct: float) -> tuple[Rating, str]:
    """主动基金综合费率评级"""
    if fee_pct < 1.0:
        return ("优秀", f"{fee_pct}%/年 < 1.0%")
    if fee_pct <= 1.5:
        return ("及格", f"1.0% ≤ {fee_pct}%/年 ≤ 1.5%")
    return ("不及格", f"{fee_pct}%/年 > 1.5%")


def rate_sharpe(sharpe: float) -> tuple[Rating, str]:
    """夏普比率评级"""
    if sharpe > 1:
        return ("优秀", f"夏普{sharpe} > 1")
    if sharpe >= 0.5:
        return ("及格", f"0.5 ≤ 夏普{sharpe} ≤ 1")
    return ("不及格", f"夏普{sharpe} < 0.5")


def rate_max_drawdown(rank: str) -> tuple[Rating, str]:
    """最大回撤评级（与同类比较）"""
    mapping = {
        "优于": ("优秀", "优于同类平均"),
        "接近": ("及格", "接近同类平均"),
        "劣于": ("不及格", "劣于同类平均"),
    }
    return mapping.get(rank, ("不及格", "数据不足"))


def rate_active_fund_all(
    excess_return: float,
    fee_pct: float,
    sharpe: float,
    drawdown_rank: str,
) -> dict:
    """主动基金全部4项质量评级"""
    results = {
        "超额收益": rate_active_excess_return(excess_return),
        "综合费率": rate_active_fund_fee(fee_pct),
        "夏普比率": rate_sharpe(sharpe),
        "最大回撤": rate_max_drawdown(drawdown_rank),
    }
    ratings = {k: v[0] for k, v in results.items()}
    passed = sum(1 for r in ratings.values() if r in ("优秀", "及格"))
    return {
        "ratings": ratings,
        "passed": passed,
        "conclusion": "✅ 通过" if passed >= 3 else "❌ 不通过",
    }
