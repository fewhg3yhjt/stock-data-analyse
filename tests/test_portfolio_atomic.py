"""Portfolio position/transaction/cash atomicity tests."""

from __future__ import annotations

import pytest

from StockInvestmentTool.portfolio.models import Position, Transaction
from StockInvestmentTool.portfolio.storage import PortfolioStorage


def test_atomic_position_transaction_rolls_back_insufficient_cash(tmp_path):
    storage = PortfolioStorage(tmp_path / "portfolio.db")

    with pytest.raises(ValueError, match="资金不足"):
        storage.atomic_position_transaction(
            Position(stock_code="sh600900", stock_name="测试", total_shares=100,
                     avg_cost=10, total_cost=1000, current_price=10, peak_price=10,
                     buy_date="2026-08-26", last_operated_date="2026-08-26"),
            Transaction(trans_type="buy", date="2026-08-26", price=10,
                        shares=100, amount=1000),
            -1000,
        )

    assert storage.get_positions() == []
    assert storage.get_portfolio().cash_available == 0


def test_atomic_position_transaction_commits_all_records(tmp_path):
    storage = PortfolioStorage(tmp_path / "portfolio.db")
    storage.set_cash(2000)
    position, txn, cash = storage.atomic_position_transaction(
        Position(stock_code="sh600900", stock_name="测试", total_shares=100,
                 avg_cost=10, total_cost=1000, current_price=10, peak_price=10,
                 buy_date="2026-08-26", last_operated_date="2026-08-26"),
        Transaction(trans_type="buy", date="2026-08-26", price=10,
                    shares=100, amount=1000),
        -1000,
    )

    assert position.id and txn.id
    assert cash == 1000
    assert len(storage.get_positions()) == 1
    assert len(storage.get_transactions(position.id)) == 1
