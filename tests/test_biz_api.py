# -*- coding: utf-8 -*-
"""新业务 API 的最小路由契约测试。"""

import os

import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("BUSINESS_DB_PATH", str(tmp_path / "business.db"))
    monkeypatch.setenv("STOCK_DISABLE_AUTH", "1")
    from StockInvestmentTool.web.app import create_app
    app = create_app()
    app.config.update(TESTING=True)
    return app


def test_biz_blueprint_registered(app):
    routes = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/api/biz/screens/preview" in routes
    assert "/api/biz/observations/<observation_id>" in routes
    assert "/api/biz/observations/<observation_id>/entry" in routes
    assert "/api/screens" in routes
    assert "/api/screen-candidates/<candidate_id>/observe" in routes
    assert "/api/observations/<observation_id>/simulation-plan" in routes
    assert "/api/watch-subscriptions" in routes
    assert "/api/portfolios/<portfolio_id>/positions" in routes
    assert "/api/position-cycles/<cycle_id>" in routes


def test_preview_requires_condition(app):
    response = app.test_client().post("/api/biz/screens/preview", json={})
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "SCREEN_INVALID"


def test_preview_requires_date_range(app):
    response = app.test_client().post("/api/biz/screens/preview", json={
        "condition_spec": {"type": "comparison"},
    })
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "SCREEN_INVALID"


def test_create_screen_validates_and_persists_definition(app):
    response = app.test_client().post("/api/screens", json={
        "name": "API筛选", "condition_spec": {"type": "comparison",
        "left": {"field": "close"}, "operator": ">", "right": {"value": 10}},
    })
    assert response.status_code == 201
    assert response.get_json()["data"]["status"] == "published"


def test_position_and_subscription_not_found_contracts(app):
    client = app.test_client()
    position = client.get("/api/position-cycles/no-such")
    assert position.status_code == 404
    assert position.get_json()["error"]["code"] == "POSITION_CYCLE_NOT_FOUND"
    portfolio = client.get("/api/portfolios/no-such/positions")
    assert portfolio.status_code == 404
    assert portfolio.get_json()["error"]["code"] == "PORTFOLIO_NOT_FOUND"


def test_observation_not_found(app):
    response = app.test_client().get("/api/biz/observations/no-such")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "OBSERVATION_NOT_FOUND"


def test_task_api_returns_persisted_requested_run(app):
    response = app.test_client().post("/api/biz/tasks/health.reconcile/runs", json={
        "input": {}, "trigger_type": "manual",
    })
    assert response.status_code == 202
    assert response.get_json()["data"]["status"] == "requested"


def test_screen_run_api_persists_run_and_candidates(app, monkeypatch):
    import pandas as pd

    from StockInvestmentTool.warehouse.datasets import DatasetResult

    data = pd.DataFrame({
        "date": pd.to_datetime(["2026-08-14"]), "code": ["sh600908"],
        "open": [10.0], "high": [12.0], "low": [9.0], "close": [11.0],
        "volume": [1000], "amount": [10000],
    })
    monkeypatch.setattr(
        "StockInvestmentTool.web.biz_api.load_market_data",
        lambda *args, **kwargs: DatasetResult(data=data, context={"quality_status": "PASS"}),
    )
    response = app.test_client().post("/api/biz/screen-runs", json={
        "screen_id": "api-screen", "name": "API筛选", "start_date": "2026-08-01",
        "as_of": "2026-08-14",
        "condition_spec": {"type": "comparison", "left": {"field": "close"},
                           "operator": ">", "right": {"value": 10}},
    })
    assert response.status_code == 201
    payload = response.get_json()["data"]
    assert payload["run"]["status"] == "success"
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0]["symbol"] == "sh600908"
