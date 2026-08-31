# -*- coding: utf-8 -*-
"""biz 包单元测试：PositionValuationService。"""

import numpy as np
import pandas as pd
import pytest

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.portfolio import EVT_BUY, PortfolioService
from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.valuation import PositionValuationService


@pytest.fixture
def pf_setup(tmp_path):
    svc = PortfolioService(BusinessRepository(BusinessDB(tmp_path / "v.db")))
    _, pf = svc.ensure_default_account_portfolio()
    svc.initialize_cash(pf.portfolio_id, 100000.0)
    cycle = svc.open_cycle(pf.portfolio_id, "sh600908")
    return svc, pf.portfolio_id, cycle.position_cycle_id


def make_price_df(symbol="sh600908", close=11.5):
    df = pd.DataFrame({
        "date": pd.bdate_range("2026-08-20", periods=3),
        "code": [symbol] * 3,
        "close": [10.0, 10.5, close],
        "open": [10.0] * 3, "high": [11.0] * 3, "low": [9.5] * 3,
        "volume": [1000] * 3, "amount": [10000] * 3,
    })
    return df


class TestValuation:
    def test_valuate_cycle(self, pf_setup):
        svc, pid, cid = pf_setup
        svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-14",
                             quantity=1000, price=10.0, fee=10.0, idempotency_key="b1")
        vs = PositionValuationService(portfolio_service=svc, df=make_price_df(close=11.5),
                                      context={"quality_status": "PASS"})
        v = vs.valuate_cycle(cid)
        assert v.quantity == 1000
        assert v.market_price == pytest.approx(11.5)
        assert v.market_value == pytest.approx(11500)
        assert v.market_price_as_of == "2026-08-24"
        assert v.price_source == "published_stock_daily"
        # unrealized = (11.5 - 10.01) * 1000 = 1490
        assert v.unrealized_pnl == pytest.approx((11.5 - 10.01) * 1000)

    def test_valuate_portfolio_aggregates(self, pf_setup):
        svc, pid, cid = pf_setup
        svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-14",
                             quantity=1000, price=10.0, idempotency_key="b1")
        vs = PositionValuationService(portfolio_service=svc, df=make_price_df(close=11.5))
        overview = vs.valuate_portfolio(pid)
        assert overview["cash_balance"] == pytest.approx(100000 - 10000)
        assert overview["market_value"] == pytest.approx(11500)
        assert overview["total_assets"] == pytest.approx(100000 - 10000 + 11500)

    def test_no_price_stale(self, pf_setup):
        svc, pid, cid = pf_setup
        svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-14",
                             quantity=1000, price=10.0, idempotency_key="b1")
        vs = PositionValuationService(portfolio_service=svc, df=pd.DataFrame())  # 无行情
        v = vs.valuate_cycle(cid)
        assert v.market_price is None
        assert v.market_value == 0.0
        assert v.market_price_as_of is None

    def test_fallback_flagged(self, pf_setup):
        svc, pid, cid = pf_setup
        svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-14",
                             quantity=1000, price=10.0, idempotency_key="b1")
        vs = PositionValuationService(portfolio_service=svc, df=make_price_df(close=11.5),
                                      context={"fallback_used": True})
        v = vs.valuate_cycle(cid)
        assert v.price_source == "fallback"

    def test_snapshot_is_persisted_and_idempotent(self, pf_setup):
        svc, pid, cid = pf_setup
        svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-14",
                             quantity=1000, price=10.0, idempotency_key="b1")
        vs = PositionValuationService(portfolio_service=svc, df=make_price_df(close=11.5))
        vs.valuate_cycle(cid)
        vs.valuate_cycle(cid)
        snapshot = vs.get_snapshot(cid, "2026-08-24")
        assert snapshot is not None
        assert snapshot["quantity"] == 1000
        assert svc.repo.db.fetchone(
            "SELECT COUNT(*) AS n FROM position_snapshots WHERE position_cycle_id=?",
            (cid,),
        )["n"] == 1
