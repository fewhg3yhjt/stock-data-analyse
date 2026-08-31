# -*- coding: utf-8 -*-
"""统一交易日服务（阶段九）。

全系统唯一的“最新已收盘交易日”入口，使用 Asia/Shanghai 时区。

- 周末固定为非交易日；
- 节假日通过可配置表维护（默认空集，可用环境变量 TRADE_HOLIDAYS 或
  load_holidays() 注入）；
- 交易日 15:35（DAILY_RUN_TIME 默认）之后视为当天已收盘。
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

DEFAULT_CLOSE_TIME = time(15, 35)


def now_shanghai() -> datetime:
    """返回当前上海时区时间（naive，语义为该时区本地时间）。"""
    from datetime import timezone

    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    except Exception:
        return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


def load_holidays(source: Optional[str] = None) -> set[date]:
    """加载节假日表。

    source 为空时读环境变量 TRADE_HOLIDAYS（逗号分隔 YYYY-MM-DD）；
    否则视为文件路径（每行一个 YYYY-MM-DD，空行/# 注释忽略）。
    """
    holidays: set[date] = set()
    if source is None:
        raw = os.getenv("TRADE_HOLIDAYS", "")
        candidates = [item.strip() for item in raw.split(",") if item.strip()]
    else:
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"节假日表不存在: {path}")
        candidates = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            candidates.append(line.split()[0])
    for item in candidates:
        try:
            holidays.add(date.fromisoformat(item))
        except ValueError:
            continue
    return holidays


HOLIDAYS: set[date] = load_holidays()


def is_trade_day(day: date) -> bool:
    """是否为交易日：非周末且不在节假日表。"""
    if day.weekday() >= 5:
        return False
    return day not in HOLIDAYS


def previous_trade_day(day: date) -> date:
    """day 之前最近的交易日（不含 day）。"""
    cursor = day - timedelta(days=1)
    while not is_trade_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def latest_closed_trade_day(now: Optional[datetime] = None,
                            close_time: Optional[time] = None) -> date:
    """最新已收盘交易日。

    上海时区；交易日收盘时刻（默认 15:35）之后返回当天，否则返回上一交易日。
    非交易日（周末/节假日）返回最近一个交易日。
    """
    current = (now or now_shanghai()).date()
    close = close_time or DEFAULT_CLOSE_TIME
    if (now or now_shanghai()).time() >= close and is_trade_day(current):
        return current
    return previous_trade_day(current)


def expected_trade_day_for_job(run_time: Optional[datetime] = None,
                               close_time: Optional[time] = None) -> date:
    """任务期望覆盖的交易日。

    与 latest_closed_trade_day 相同：交易日 15:35 后运行则覆盖当天，
    否则覆盖上一交易日。
    """
    return latest_closed_trade_day(run_time, close_time=close_time)