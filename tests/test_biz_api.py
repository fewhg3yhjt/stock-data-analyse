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


def test_preview_requires_condition(app):
    response = app.test_client().post("/api/biz/screens/preview", json={})
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "SCREEN_INVALID"


def test_observation_not_found(app):
    response = app.test_client().get("/api/biz/observations/no-such")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "OBSERVATION_NOT_FOUND"
