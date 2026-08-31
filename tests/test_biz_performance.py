# -*- coding: utf-8 -*-
"""biz 包单元测试：PerformanceService / ReviewService。"""

import numpy as np
import pandas as pd
import pytest

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.performance import PerformanceService, ReviewService
from StockInvestmentTool.biz.portfolio import EVT_BUY, EVT_CASH_DIVIDEND, PortfolioService
from StockInvestmentTool.biz.repo import BusinessRepository


@pytest.fixture
def setup(tmp_path):
    svc = PortfolioService(BusinessRepository(BusinessDB(tmp_path / "perf.db")))
    _, pf = svc.ensure_default_account_portfolio()
    svc.initialize_cash(pf.portfolio_id, 100000.0)
    cycle = svc.open_cycle(pf.portfolio_id, "sh600908")
    return svc, pf.portfolio_id, cycle.position_cycle_id


def make_price_df():
    n = 5
    dates = pd.bdate_range("2026-08-20", periods=n)
    df = pd.DataFrame({
        "date": dates, "code": ["sh600908"] * n,
        "close": [10.0, 10.5, 11.0, 11.5, 12.0],
        "open": [10.0] * n, "high": [12.0] * n, "low": [9.5] * n,
        "volume": [1000] * n, "amount": [10000] * n,
    })
    return df


class TestPerformance:
    def test_compute_with_benchmark(self, setup):
        svc, pid, cid = setup
        svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-20T09:30:00Z",
                             quantity=1000, price=10.0, idempotency_key="b1")
        ps = PerformanceService(svc)
        result = ps.compute(pid, "2026-08-20", "2026-08-26", price_df=make_price_df(),
                            benchmark_return=0.05)
        assert result.equity_curve
        assert result.comparison_status == "ok"
        assert result.total_return is not None
        assert result.excess_return == pytest.approx(result.total_return - 0.05)
        assert result.max_drawdown is not None

    def test_benchmark_unavailable(self, setup):
        svc, pid, cid = setup
        svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-20",
                             quantity=1000, price=10.0, idempotency_key="b1")
        ps = PerformanceService(svc)
        result = ps.compute(pid, "2026-08-20", "2026-08-26", price_df=make_price_df())
        assert result.comparison_status == "unavailable"
        assert result.benchmark_return is None

    def test_equity_curve_points(self, setup):
        svc, pid, cid = setup
        svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-20T09:30:00Z",
                             quantity=1000, price=10.0, idempotency_key="b1")
        svc.record_execution(pid, cid, event_type=EVT_CASH_DIVIDEND, trade_time="2026-08-24T09:30:00Z",
                             quantity=200.0, idempotency_key="div1")
        ps = PerformanceService(svc)
        curve = ps.equity_curve(pid, price_df=make_price_df())
        assert curve
        assert all(p.equity >= 0 for p in curve)

    def test_max_drawdown(self):
        assert PerformanceService._max_drawdown([100, 90, 95]) == pytest.approx(0.1)


class TestReview:
    def test_create_and_get(self, tmp_path):
        svc = ReviewService(BusinessRepository(BusinessDB(tmp_path / "rev.db")))
        review = svc.create_review("pc1", discovery_reason="趋势回踩", simulation_run_id="sr1")
        got = svc.get_review(review.review_id)
        assert got["position_cycle_id"] == "pc1"
        assert got["simulation_run_id"] == "sr1"

    def test_update_and_evidence(self, tmp_path):
        svc = ReviewService(BusinessRepository(BusinessDB(tmp_path / "rev2.db")))
        review = svc.create_review("pc1")
        review.lessons = "择时偏差"
        review.execution_deviation = {"entry_price_deviation": 0.02}
        svc.update_review(review)
        got = svc.get_review(review.review_id)
        assert got["lessons"] == "择时偏差"
        assert got["execution_deviation"]["entry_price_deviation"] == 0.02
        eid = svc.add_evidence(review.review_id, source_type="simulation_run", source_id="sr1",
                               summary="模拟收益 8%", snapshot={"return": 0.08})
        evs = svc.list_evidence(review.review_id)
        assert len(evs) == 1
        assert evs[0]["evidence_id"] == eid