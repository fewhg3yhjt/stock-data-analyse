from __future__ import annotations

import pandas as pd

from StockInvestmentTool.fundflow.analysis import sector_rotation_view


def test_sector_rotation_view_explains_price_money_alignment():
    now = pd.DataFrame([
        {"name": "半导体", "chg": 2.3, "net": 3.2, "leader": "甲", "leader_chg": 5.0},
        {"name": "房地产", "chg": 1.2, "net": -1.0, "leader": "乙", "leader_chg": 2.0},
    ])
    days = pd.DataFrame([
        {"name": "半导体", "net": 5.5},
        {"name": "房地产", "net": -2.0},
    ])

    result = sector_rotation_view(now, days)

    assert result[0]["state"] == "强势上行"
    assert result[0]["advice"] == "重点观察，等待回踩确认"
    assert result[1]["state"] == "价涨钱走"
    assert result[1]["advice"] == "谨慎追高，等待资金重新配合"


def test_live_rotation_route_returns_online_research_contract(monkeypatch):
    from StockInvestmentTool.web.app import create_app
    from StockInvestmentTool.fundflow import sources

    now = pd.DataFrame([{"name": "半导体", "chg": 2.3, "net": 3.2}])
    days = pd.DataFrame([{"name": "半导体", "net": 5.5}])
    monkeypatch.setenv("STOCK_DISABLE_AUTH", "1")
    monkeypatch.setattr(sources, "fetch_sector", lambda kind, period: now if period == "now" else days)

    response = create_app().test_client().get("/market/live_rotation?kind=industry")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data_type"] == "online_research_only"
    assert payload["items"][0]["state"] == "强势上行"
    assert "不作为正式策略输入" in payload["warning"]


def test_live_rotation_route_rejects_unknown_kind(monkeypatch):
    from StockInvestmentTool.web.app import create_app

    monkeypatch.setenv("STOCK_DISABLE_AUTH", "1")
    response = create_app().test_client().get("/market/live_rotation?kind=unknown")

    assert response.status_code == 400
    assert response.get_json()["status"] == "error"
