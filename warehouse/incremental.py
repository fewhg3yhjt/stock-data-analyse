"""Bounded lookback planning for derived indicator/factor recalculation."""

from __future__ import annotations

import pandas as pd


MAX_LOOKBACK_TRADING_DAYS = 250


def affected_window(changed_start: str, changed_end: str, *, lookback_days: int = MAX_LOOKBACK_TRADING_DAYS) -> tuple[str, str]:
    """Return a conservative calendar window covering the required trading lookback."""
    start = pd.Timestamp(changed_start)
    end = pd.Timestamp(changed_end)
    if end < start:
        raise ValueError("changed_end 不能早于 changed_start")
    return (str((start - pd.Timedelta(days=max(1, lookback_days * 2))).date()), str(end.date()))


def affected_partitions(changed_start: str, changed_end: str) -> list[str]:
    start, end = affected_window(changed_start, changed_end)
    return [item.strftime("%Y-%m") for item in pd.date_range(pd.Timestamp(start).replace(day=1), pd.Timestamp(end).replace(day=1), freq="MS")]
