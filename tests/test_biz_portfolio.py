# -*- coding: utf-8 -*-
"""biz 包单元测试：账户持仓与交易（PortfolioService）。"""

import pytest

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.portfolio import (
    EVT_BUY,
    EVT_CASH_ADJUSTMENT,
    EVT_CASH_DIVIDEND,
    EVT_SELL,
    PortfolioService,
)
from StockInvestmentTool.biz.repo import BusinessRepository


@pytest.fixture
def svc(tmp_path):
    return PortfolioService(BusinessRepository(BusinessDB(tmp_path / "pf.db")))


@pytest.fixture
def setup(svc):
    """返回 (portfolio_id, cycle_id)，初始现金 100000。"""
    _, pf = svc.ensure_default_account_portfolio()
    svc.initialize_cash(pf.portfolio_id, 100000.0)
    cycle = svc.open_cycle(pf.portfolio_id, "sh600908", strategy_version_id="sv1")
    return pf.portfolio_id, cycle.position_cycle_id


class TestPortfolio:
    def test_default_account_created(self, svc):
        acc, pf = svc.ensure_default_account_portfolio()
        assert acc.account_type == "real"
        assert pf.account_id == acc.account_id
        # 幂等
        acc2, pf2 = svc.ensure_default_account_portfolio()
        assert pf2.portfolio_id == pf.portfolio_id

    def test_buy_updates_cash_and_lots(self, svc, setup):
        pid, cid = setup
        exe = svc.record_execution(
            pid, cid, event_type=EVT_BUY, trade_time="2026-08-14T10:00:00Z",
            quantity=1000, price=10.0, fee=10.0, idempotency_key="buy1")
        assert svc.cash_balance(pid) == pytest.approx(100000 - 1000 * 10 - 10)
        summary = svc.position_summary(cid)
        assert summary["quantity"] == 1000
        assert summary["average_cost"] == pytest.approx(10.0 + 10 / 1000)

    def test_idempotent_execution(self, svc, setup):
        pid, cid = setup
        e1 = svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-14",
                                  quantity=1000, price=10.0, idempotency_key="dup")
        e2 = svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-14",
                                  quantity=1000, price=10.0, idempotency_key="dup")
        assert e1.execution_id == e2.execution_id
        assert svc.cash_balance(pid) == pytest.approx(100000 - 1000 * 10)

    def test_sell_fifo_and_close(self, svc, setup):
        pid, cid = setup
        svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-14",
                             quantity=1000, price=10.0, fee=10.0, idempotency_key="b1")
        svc.record_execution(pid, cid, event_type=EVT_SELL, trade_time="2026-08-20",
                             quantity=1000, price=12.0, fee=12.0, idempotency_key="s1")
        summary = svc.position_summary(cid)
        assert summary["quantity"] == 0
        cycle = svc.get_cycle(cid)
        assert cycle.status == "closed"
        assert cycle.phase == "closed"
        # 已实现收益 = 1000*12 - 12 - (1000*10 + 10) = 1978
        assert summary["realized_pnl"] == pytest.approx(12000 - 12 - 10010)

    def test_sell_over_position_raises(self, svc, setup):
        pid, cid = setup
        svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-14",
                             quantity=1000, price=10.0, idempotency_key="b1")
        with pytest.raises(ValueError):
            svc.record_execution(pid, cid, event_type=EVT_SELL, trade_time="2026-08-20",
                                 quantity=2000, price=12.0, idempotency_key="s1")

    def test_insufficient_cash_raises(self, svc, setup):
        pid, cid = setup
        with pytest.raises(ValueError):
            svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-14",
                                 quantity=20000, price=100.0, idempotency_key="big")

    def test_dividend_adds_cash(self, svc, setup):
        pid, cid = setup
        svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-14",
                             quantity=1000, price=10.0, idempotency_key="b1")
        cash_before = svc.cash_balance(pid)
        svc.record_execution(pid, cid, event_type=EVT_CASH_DIVIDEND, trade_time="2026-09-01",
                             quantity=500.0, price=None, idempotency_key="div1")
        assert svc.cash_balance(pid) == pytest.approx(cash_before + 500)

    def test_cash_adjustment(self, svc, setup):
        pid, cid = setup
        svc.record_execution(pid, cid, event_type=EVT_CASH_ADJUSTMENT, trade_time="2026-08-15",
                             quantity=-1000.0, idempotency_key="adj1")
        assert svc.cash_balance(pid) == pytest.approx(100000 - 1000)

    def test_fifo_consumes_earliest_lot(self, svc, setup):
        pid, cid = setup
        svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-01",
                             quantity=500, price=10.0, idempotency_key="b1")
        svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-05",
                             quantity=500, price=12.0, idempotency_key="b2")
        svc.record_execution(pid, cid, event_type=EVT_SELL, trade_time="2026-08-10",
                             quantity=500, price=11.0, idempotency_key="s1")
        summary = svc.position_summary(cid)
        assert summary["quantity"] == 500
        # 剩余为第二批（price 12），FIFO 消耗第一批
        assert summary["average_cost"] == pytest.approx(12.0)

    def test_realized_pnl_only_uses_sold_fifo_lot(self, svc, setup):
        pid, cid = setup
        svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-01",
                             quantity=500, price=10.0, fee=5.0, idempotency_key="b1")
        svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-05",
                             quantity=500, price=20.0, fee=10.0, idempotency_key="b2")
        svc.record_execution(pid, cid, event_type=EVT_SELL, trade_time="2026-08-10",
                             quantity=500, price=12.0, fee=6.0, idempotency_key="s1")
        summary = svc.position_summary(cid)
        assert summary["quantity"] == 500
        # 500 * 12 - FIFO cost 500 * 10 - buy fee 5 - sell fee 6 = 989
        assert summary["realized_pnl"] == pytest.approx(989.0)
        assert svc.repo.db.fetchone(
            "SELECT COUNT(*) AS n FROM execution_lot_allocations"
        )["n"] == 1

    def test_cash_never_negative_silent(self, svc, setup):
        pid, cid = setup
        with pytest.raises(ValueError):
            svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-14",
                                 quantity=100000, price=2.0, idempotency_key="x")
        assert svc.cash_balance(pid) == pytest.approx(100000)  # 事务未污染

        # 失败动作不能留下孤立 Execution 或 Lot
        assert svc.repo.db.fetchone(
            "SELECT COUNT(*) AS n FROM executions WHERE idempotency_key='x'"
        )["n"] == 0
        assert svc.repo.db.fetchone(
            "SELECT COUNT(*) AS n FROM position_lots WHERE position_cycle_id=?", (cid,)
        )["n"] == 0

    def test_transaction_rolls_back_after_execution_insert(self, svc, setup, monkeypatch):
        pid, cid = setup

        def fail_after_insert(conn, exe):
            raise RuntimeError("injected failure")

        monkeypatch.setattr(svc, "_apply_buy_conn", fail_after_insert)
        with pytest.raises(RuntimeError):
            svc.record_execution(pid, cid, event_type=EVT_BUY, trade_time="2026-08-14",
                                 quantity=1000, price=10.0, idempotency_key="rollback")

        assert svc.repo.db.fetchone(
            "SELECT COUNT(*) AS n FROM executions WHERE idempotency_key='rollback'"
        )["n"] == 0
        assert svc.cash_balance(pid) == pytest.approx(100000)
