"""Isolated V11 minute-path injection tests."""

from __future__ import annotations

import json

import pandas as pd

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.minute_take_profit_v11 import V11_EVENT_TYPE, evaluate
from StockInvestmentTool.biz.portfolio import EVT_BUY, PortfolioService
from StockInvestmentTool.biz.models import now_utc
from StockInvestmentTool.biz.position_runtime import PositionRuntimeService, PositionRuntimeState
from StockInvestmentTool.biz.repo import BusinessRepository


def _bars(closes: list[float], *, start: str = "2026-09-07 09:30") -> pd.DataFrame:
    times = pd.date_range(start, periods=len(closes), freq="min")
    return pd.DataFrame({
        "time": times,
        "close": closes,
        "volume": [100.0] * len(closes),
        "amount": [1000.0] * len(closes),
    })


def _setup(tmp_path):
    repo = BusinessRepository(BusinessDB(tmp_path / "v11.db"))
    portfolio = PortfolioService(repo)
    _, portfolio_obj = portfolio.ensure_default_account_portfolio()
    portfolio.initialize_cash(portfolio_obj.portfolio_id, 100000.0)
    cycle = portfolio.open_cycle(portfolio_obj.portfolio_id, "sz.000400", strategy_version_id="minute_take_profit_v11")
    portfolio.record_execution(
        portfolio_obj.portfolio_id, cycle.position_cycle_id, event_type=EVT_BUY,
        trade_time="2026-09-07", quantity=100, price=100.0,
        idempotency_key="v11-buy",
    )
    return repo, cycle


def test_v11_holds_normal_pullback_and_notifies_on_confirmed_reversal(tmp_path, monkeypatch):
    repo, cycle = _setup(tmp_path)
    monkeypatch.setenv("POSITION_V11_MODE", "notify")
    monkeypatch.setattr(
        "StockInvestmentTool.biz.minute_take_profit_v11._daily_context",
        lambda symbol, day: (100.0, 2.0),
    )
    paths = iter([
        _bars([100.0, 101.0, 102.0, 103.0]),
        _bars([100.0, 101.0, 102.0, 103.0, 102.5]),
        _bars([100.0, 101.0, 102.0, 103.0, 102.0, 101.0, 99.0]),
    ])
    monkeypatch.setattr(
        "StockInvestmentTool.biz.minute_take_profit_v11._minute_frame",
        lambda symbol, day=None: next(paths),
    )

    cycle_data = {"symbol": cycle.symbol, "position_cycle_id": cycle.position_cycle_id,
                  "average_cost": 100.0}
    first = evaluate(cycle_data, current_price=103.0)
    assert first["state"] == "HOLD"
    second = evaluate(cycle_data, current_price=102.5, prior_context=first["context"])
    assert second["state"] == "HOLD"
    assert second["notify"] is False
    third = evaluate(cycle_data, current_price=99.0, prior_context=second["context"])
    assert third["state"] in {"STOP_LOSS", "TAKE_PROFIT_PENDING", "TAKE_PROFIT"}
    assert third["notify"] is True


def test_v11_runtime_writes_context_and_notify_event_without_sell_action(tmp_path, monkeypatch):
    repo, cycle = _setup(tmp_path)
    monkeypatch.setenv("POSITION_V11_MODE", "notify")
    monkeypatch.setattr(
        "StockInvestmentTool.biz.minute_take_profit_v11._daily_context",
        lambda symbol, day: (100.0, 2.0),
    )
    monkeypatch.setattr(
        "StockInvestmentTool.biz.minute_take_profit_v11._minute_frame",
        lambda symbol, day=None: _bars([100.0, 103.0, 102.0, 101.0, 99.0]),
    )
    monkeypatch.setattr(
        PositionRuntimeService,
        "_compute_state",
        lambda self, current_cycle, price, price_date, price_source, prior: PositionRuntimeState(
            position_cycle_id=current_cycle["position_cycle_id"], symbol=current_cycle["symbol"],
            current_price=price, highest_since_entry=price, lowest_since_entry=price,
            unrealized_pnl=-100.0, unrealized_pnl_pct=-0.01, max_profit_pct=0.03,
            drawdown_from_high=0.0, holding_days=0, price_as_of=price_date,
            price_source=price_source, data_context={"average_cost": 100.0}, updated_at=now_utc(),
        ),
    )
    service = PositionRuntimeService(repo, price_loader=type("Loader", (), {
        "latest_price": lambda self, symbol: (99.0, "2026-09-07 09:34", "minute"),
    })())
    result = service.evaluate_cycle(cycle.position_cycle_id)

    assert result["v11"]["notify"] is True
    row = repo.db.fetchone(
        "SELECT data_context_json FROM position_runtime_states WHERE position_cycle_id=?",
        (cycle.position_cycle_id,),
    )
    context = json.loads(row["data_context_json"])
    assert context["v11_status"] in {"STOP_LOSS", "TAKE_PROFIT_PENDING", "TAKE_PROFIT"}
    event = repo.db.fetchone(
        "SELECT event_type, payload_json FROM notification_events WHERE event_type=?",
        (V11_EVENT_TYPE,),
    )
    assert event is not None
    payload = json.loads(event["payload_json"])
    assert payload["action"] == "NOTIFY"
    assert "SELL_ALL" not in payload["text"]
