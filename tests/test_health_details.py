"""Authenticated operational health details endpoint tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def client():
    from StockInvestmentTool.web.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["admin"] = True
    return client


def test_health_details(client):
    response = client.get("/api/health/details")

    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "success"
    assert "daily_partitions" in data["warehouse"]
    assert "minute_snapshot" in data["features"]
    assert "notifications" in data
    assert "outbox" in data["notifications"]
