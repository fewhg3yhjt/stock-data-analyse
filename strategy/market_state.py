"""市场状态判定 — 对齐个股 v4.5 Part4.三

Prompt原文:
    牛市初期: 从近6个月低点反弹<15%，且均线呈多头排列初期
    牛市中期: 从近6个月低点反弹15%-30%，均线多头排列
    牛市末期: 从近6个月低点反弹>30%，或价格接近52周最高(≥95%)
    震荡市:   均线粘合，价格在区间内反复
    熊市/下跌趋势: 均线空头排列
"""

from typing import Optional

import pandas as pd


def determine_market_state(
    rebound_pct: float,
    trend: str,
    price: float,
    year_high: float,
) -> str:
    """根据 Prompt 规则判定市场状态（5态）

    Args:
        rebound_pct: 从近6个月低点的反弹幅度(%)
        trend: 均线排列判定结果（来自 TechnicalIndicators.trend_judgment）
        price: 当前价格
        year_high: 近12个月最高价

    Returns:
        市场状态: 牛市初期 / 牛市中期 / 牛市末期 / 震荡市 / 熊市/下跌趋势
    """
    # 空头 → 熊市
    if "空头" in trend and "MA5 < MA20 < MA60" in trend:
        return "熊市/下跌趋势"

    # 震荡 → 震荡市
    if trend == "震荡格局":
        return "震荡市"

    # 多头排列下的3档判定（Prompt: >30%为牛市末期）
    if rebound_pct > 30 or price >= year_high * 0.95:
        return "牛市末期"

    if rebound_pct >= 15:
        return "牛市中期"

    return "牛市初期"


def determine_market_state_from_df(
    df: pd.DataFrame,
    months_low_window: int = 126,  # 近6个月约126个交易日
) -> str:
    """从K线数据直接判定当前市场状态

    Args:
        df: K线DataFrame（需含close, high列）
        months_low_window: 计算低点的窗口期

    Returns:
        市场状态
    """
    from StockInvestmentTool.datasource.indicators import TechnicalIndicators

    if df is None or len(df) == 0:
        return "数据不足"

    last = df.iloc[-1]
    trend = TechnicalIndicators.trend_judgment(df)

    # 近6个月低点
    recent = df.tail(months_low_window)
    low_6m = recent["low"].min()
    price = last["close"]

    rebound_pct = (price - low_6m) / low_6m * 100 if low_6m > 0 else 0
    year_high = df["high"].max()

    return determine_market_state(rebound_pct, trend, price, year_high)


# ── 看板专用 4 态判定（四维一体 v5.1 设计）──────────────
# 修正设计问题 1/2:
#   ① 强多 = MA5>MA20>MA60 且 MA20 斜率向上（去掉"反弹<30%"矛盾条件）
#   ② 判定优先级固定: 先判排列 → 再按斜率拆分强/弱多 → 最后判震荡

def dashboard_market_state(df: pd.DataFrame, ma_slope_days: int = 5) -> str:
    """按看板设计返回 4 态市场状态: 强多 / 弱多 / 震荡 / 空头。

    依赖 K 线的 ma5/ma20/ma60 列（用 TechnicalIndicators.compute_all 预计算）。
    """
    from StockInvestmentTool.datasource.indicators import TechnicalIndicators

    if df is None or len(df) == 0:
        return "数据不足"

    last = df.iloc[-1]
    trend = TechnicalIndicators.trend_judgment(df)

    # 空头排列 → 空头
    if "空头" in trend:
        return "空头"

    # 多头排列: 按 MA20 斜率拆分强/弱多
    if "多头" in trend:
        slope = TechnicalIndicators.ma_slope(
            df, ma_col="ma20", compare_days=ma_slope_days
        )
        return "强多" if slope == "向上" else "弱多"

    # 震荡格局 → 震荡
    if trend == "震荡格局":
        return "震荡"

    return "震荡"  # 数据不足也按震荡处理，不阻断流程

