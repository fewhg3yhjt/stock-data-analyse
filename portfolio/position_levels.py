"""持仓点位的统一计算口径。"""

from __future__ import annotations

from typing import Optional

import pandas as pd


DEFAULT_YEAR_HIGH_WINDOW = 252


def calculate_year_high(frame: pd.DataFrame, *, window: int = DEFAULT_YEAR_HIGH_WINDOW,
                        price_field: str = "high") -> Optional[float]:
    """计算滚动前高：最近 window 个交易日指定价格字段的最高值。"""
    if frame is None or frame.empty or price_field not in frame:
        return None
    window = max(1, int(window))
    values = pd.to_numeric(frame[price_field], errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.tail(window).max())


def calculate_position_drawdown(peak_price: float | None,
                                current_price: float | None) -> Optional[float]:
    """当前价相对持仓峰值的回撤比例（正数表示回撤）。"""
    if peak_price in (None, 0) or current_price is None:
        return None
    return (float(peak_price) - float(current_price)) / float(peak_price)


def calculate_right_side_trigger_price(peak_price: float | None,
                                       drawdown_threshold: float | None) -> Optional[float]:
    """右侧移动止盈触发价 = 持仓峰值 × (1 - 回撤阈值)。"""
    if peak_price in (None, 0) or drawdown_threshold is None:
        return None
    return round(float(peak_price) * (1 - float(drawdown_threshold)), 2)


def calculate_position_peak(daily: pd.DataFrame | None = None,
                            minute: pd.DataFrame | None = None,
                            *, buy_date: str = "", buy_price: float = 0,
                            current_price: float | None = None) -> dict:
    """计算持仓以来峰值。

    口径：建仓日及以后，日线收盘价与分钟实时 close 的最大值；
    buy_price/current_price 作为没有完整行情时的边界值。
    """
    buy_day = pd.to_datetime(buy_date, errors="coerce")
    candidates: list[tuple[float, str, str]] = []
    if buy_price and float(buy_price) > 0:
        candidates.append((float(buy_price), str(buy_date)[:10], "buy"))
    if daily is not None and not daily.empty and "date" in daily and "close" in daily:
        frame = daily.copy()
        frame["_date"] = pd.to_datetime(frame["date"], errors="coerce")
        if pd.notna(buy_day):
            frame = frame[frame["_date"] >= buy_day.normalize()]
        for _, row in frame.dropna(subset=["_date"]).iterrows():
            value = pd.to_numeric(row.get("close"), errors="coerce")
            if pd.notna(value):
                candidates.append((float(value), row["_date"].strftime("%Y-%m-%d"), "daily_close"))
    if minute is not None and not minute.empty and "close" in minute:
        frame = minute.copy()
        time_col = "time" if "time" in frame.columns else "trade_date"
        frame["_time"] = pd.to_datetime(frame[time_col], errors="coerce")
        if pd.notna(buy_day):
            frame = frame[frame["_time"] >= buy_day.normalize()]
        for _, row in frame.dropna(subset=["_time"]).iterrows():
            value = pd.to_numeric(row.get("close"), errors="coerce")
            if pd.notna(value):
                candidates.append((float(value), row["_time"].strftime("%Y-%m-%d %H:%M:%S"), "minute_close"))
    if current_price not in (None, 0):
        candidates.append((float(current_price), "now", "realtime"))
    if not candidates:
        return {"value": None, "at": None, "source": None}
    value, stamp, source = max(candidates, key=lambda item: item[0])
    return {"value": round(value, 4), "at": stamp, "source": source}
