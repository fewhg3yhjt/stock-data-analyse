# -*- coding: utf-8 -*-
"""阶段九：统一交易日服务测试。"""

from datetime import date, datetime, time

import pytest

from StockInvestmentTool.ops.trade_calendar import (
    is_trade_day,
    latest_closed_trade_day,
    previous_trade_day,
)


def _set_holidays(monkeypatch, days):
    from StockInvestmentTool.ops import trade_calendar

    monkeypatch.setattr(trade_calendar, "HOLIDAYS", set(days))


def test_weekend_is_not_trade_day():
    assert not is_trade_day(date(2026, 8, 29))  # 周六
    assert not is_trade_day(date(2026, 8, 30))  # 周日
    assert is_trade_day(date(2026, 8, 28))  # 周五


def test_holiday_is_not_trade_day(monkeypatch):
    _set_holidays(monkeypatch, [date(2026, 9, 1)])
    assert not is_trade_day(date(2026, 9, 1))
    assert is_trade_day(date(2026, 9, 2))


def test_previous_trade_day_skips_weekend():
    # 周一 8-31 → 上一交易日为周五 8-28
    assert previous_trade_day(date(2026, 8, 31)) == date(2026, 8, 28)


def test_previous_trade_day_skips_holiday(monkeypatch):
    _set_holidays(monkeypatch, [date(2026, 8, 28)])
    # 周五 8-28 放假 → 8-31 的上一个交易日是 8-27
    assert previous_trade_day(date(2026, 8, 31)) == date(2026, 8, 27)


def test_close_after_returns_same_day():
    # 周四交易日 15:35 之后 → 返回当天
    assert latest_closed_trade_day(datetime(2026, 8, 27, 15, 35)) == date(2026, 8, 27)
    assert latest_closed_trade_day(datetime(2026, 8, 27, 21)) == date(2026, 8, 27)


def test_before_close_returns_previous_day():
    # 周四交易日盘前 → 返回周三
    assert latest_closed_trade_day(datetime(2026, 8, 27, 10)) == date(2026, 8, 26)


def test_weekend_returns_friday():
    # 周日 → 最近交易日周五
    assert latest_closed_trade_day(datetime(2026, 8, 30, 21)) == date(2026, 8, 28)


def test_holiday_returns_previous_trade_day(monkeypatch):
    _set_holidays(monkeypatch, [date(2026, 9, 1)])
    assert latest_closed_trade_day(datetime(2026, 9, 1, 21)) == date(2026, 8, 31)


def test_expected_trade_day_for_job_matches_closed_day():
    from StockInvestmentTool.ops.trade_calendar import expected_trade_day_for_job

    assert expected_trade_day_for_job(datetime(2026, 8, 27, 21)) == date(2026, 8, 27)
    assert expected_trade_day_for_job(datetime(2026, 8, 27, 10)) == date(2026, 8, 26)


def test_load_holidays_from_env(monkeypatch, tmp_path):
    from StockInvestmentTool.ops.trade_calendar import load_holidays

    path = tmp_path / "holidays.txt"
    path.write_text("# 2026 节假日\n2026-09-01\n2026-09-02 extra\n\n2026-10-01\n", encoding="utf-8")
    days = load_holidays(str(path))
    assert days == {date(2026, 9, 1), date(2026, 9, 2), date(2026, 10, 1)}