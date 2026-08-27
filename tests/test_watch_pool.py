from __future__ import annotations

from types import SimpleNamespace

from StockInvestmentTool.portfolio.dashboard import DashboardService


def test_watch_pool_merges_sources_and_action(monkeypatch):
    watch = SimpleNamespace(stock_code="sh.600900", stock_name="测试", source="manual",
                            to_dict=lambda: {"stock_code": "sh.600900", "source": "manual"})
    simulation = SimpleNamespace(stock_code="sh600900", stock_name="测试", to_dict=lambda: {
        "stock_code": "sh600900", "scheme_name": "default_value", "current_price": 10,
    })
    holding = SimpleNamespace(stock_code="sh600900", stock_name="测试", to_dict=lambda: {
        "stock_code": "sh600900", "total_shares": 100, "unrealized_pnl_pct": 3,
    })
    manager = SimpleNamespace(get_watchlist=lambda: [watch], get_simulations=lambda: [simulation],
                              storage=SimpleNamespace(get_open_positions=lambda: [holding]))
    service = DashboardService(manager)
    monkeypatch.setattr(service, "observe_pool", lambda use_cache=True: [])
    items = service.watch_pool()
    assert len(items) == 1
    assert items[0]["sources"] == ["manual", "holding"]
    assert items[0]["simulation"] is not None
    assert items[0]["next_action"] == "review"


def test_strategy_candidate_must_enter_observation_first(monkeypatch):
    manager = SimpleNamespace(get_watchlist=lambda: [], get_simulations=lambda: [],
                              storage=SimpleNamespace(get_open_positions=lambda: []))
    service = DashboardService(manager)
    monkeypatch.setattr(service, "observe_pool", lambda use_cache=True: [{"code": "sh600900", "name": "候选"}])
    items = service.watch_pool()
    assert items[0]["next_action"] == "observe"
