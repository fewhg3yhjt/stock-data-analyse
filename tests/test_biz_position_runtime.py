# -*- coding: utf-8 -*-
"""持仓运行状态与回撤通知（PositionRuntimeService）测试。"""

import pytest

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.portfolio import EVT_BUY, PortfolioService
from StockInvestmentTool.biz.position_runtime import (
    DEFAULT_DRAWDOWN_THRESHOLD,
    POSITION_DRAWDOWN_EVENT_TYPE,
    PositionRuntimeService,
    drawdown_threshold,
)
from StockInvestmentTool.biz.repo import BusinessRepository


class FakePriceLoader:
    """可编程价格源：minute 优先 / daily 兜底。"""

    def __init__(self, minute=None, daily=None, source="minute"):
        self.minute = minute
        self.daily = daily
        self.source = source

    def latest_price(self, symbol):
        if self.source == "minute":
            return self.minute, "2026-09-01", "minute"
        return self.daily, "2026-09-01", "daily"


@pytest.fixture
def setup(tmp_path):
    repo = BusinessRepository(BusinessDB(tmp_path / "runtime.db"))
    svc = PortfolioService(repo)
    _, pf = svc.ensure_default_account_portfolio()
    svc.initialize_cash(pf.portfolio_id, 100000.0)
    cycle = svc.open_cycle(pf.portfolio_id, "sh600908", strategy_version_id="sv1")
    svc.record_execution(
        pf.portfolio_id, cycle.position_cycle_id, event_type=EVT_BUY,
        trade_time="2026-09-01", quantity=1000, price=10.0, idempotency_key="buy1",
    )
    return repo, pf.portfolio_id, cycle.position_cycle_id


def test_drawdown_threshold_default():
    assert DEFAULT_DRAWDOWN_THRESHOLD == 0.02
    assert drawdown_threshold() == DEFAULT_DRAWDOWN_THRESHOLD


def test_evaluate_writes_runtime_state(setup):
    repo, _, cycle_id = setup
    loader = FakePriceLoader(minute=9.5)
    result = PositionRuntimeService(repo, price_loader=loader).evaluate_cycle(cycle_id)
    state = result["state"]
    assert result["triggered"] is False
    assert state["symbol"] == "sh600908"
    assert state["current_price"] == 9.5
    # 首轮评估后高=现价，回撤为 0
    assert state["drawdown_from_high"] == 0.0
    assert state["price_source"] == "minute"

    row = repo.db.fetchone(
        "SELECT * FROM position_runtime_states WHERE position_cycle_id=?", (cycle_id,)
    )
    assert row is not None
    assert row["symbol"] == "sh600908"
    assert row["highest_since_entry"] == pytest.approx(9.5)


def test_high_drawdown_triggers_event(setup):
    repo, _, cycle_id = setup
    # 先建立后高
    PositionRuntimeService(repo, price_loader=FakePriceLoader(minute=10.0)).evaluate_cycle(cycle_id)
    # 回落触发回撤
    result = PositionRuntimeService(repo, price_loader=FakePriceLoader(minute=9.0)).evaluate_cycle(cycle_id, threshold=0.02)
    assert result["triggered"] is True

    events = repo.db.fetchall(
        "SELECT * FROM notification_events WHERE event_type=?", (POSITION_DRAWDOWN_EVENT_TYPE,)
    )
    assert len(events) == 1
    assert events[0]["symbol"] == "sh600908"
    assert events[0]["subject_id"] == cycle_id


def test_drawdown_event_dedupe_by_day(setup):
    """同一持仓同一数据日重复评估不产生重复事件。"""
    repo, _, cycle_id = setup
    svc = PositionRuntimeService(repo, price_loader=FakePriceLoader(minute=10.0))
    svc.evaluate_cycle(cycle_id, threshold=0.02)
    svc = PositionRuntimeService(repo, price_loader=FakePriceLoader(minute=9.0))
    svc.evaluate_cycle(cycle_id, threshold=0.02)
    svc = PositionRuntimeService(repo, price_loader=FakePriceLoader(minute=9.0))
    svc.evaluate_cycle(cycle_id, threshold=0.02)
    events = repo.db.fetchall(
        "SELECT * FROM notification_events WHERE event_type=?", (POSITION_DRAWDOWN_EVENT_TYPE,)
    )
    assert len(events) == 1


def test_daily_fallback_price_source(setup):
    repo, _, cycle_id = setup
    loader = FakePriceLoader(minute=None, daily=9.8, source="daily")
    result = PositionRuntimeService(repo, price_loader=loader).evaluate_cycle(cycle_id)
    assert result["state"]["price_source"] == "daily"
    assert result["state"]["current_price"] == 9.8


def test_highest_since_entry_incremental(setup):
    repo, _, cycle_id = setup
    svc = PositionRuntimeService(repo)
    # 第一轮：高价
    svc.evaluate_cycle(cycle_id, threshold=0.02)
    loader = FakePriceLoader(minute=11.0)
    result = PositionRuntimeService(repo, price_loader=loader).evaluate_cycle(cycle_id)
    assert result["state"]["highest_since_entry"] == 11.0
    assert result["state"]["max_profit_pct"] == pytest.approx(0.10, rel=1e-3)
    # 第二轮：回落到 10.5，回撤相对 11.0 计算
    loader2 = FakePriceLoader(minute=10.5)
    result2 = PositionRuntimeService(repo, price_loader=loader2).evaluate_cycle(cycle_id)
    assert result2["state"]["highest_since_entry"] == 11.0
    assert result2["state"]["drawdown_from_high"] == pytest.approx(-0.04545, rel=1e-3)