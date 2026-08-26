"""Notification delivery ledger API tests."""

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


def test_notify_outbox_api(client):
    response = client.get("/api/notify/outbox")

    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "success"
    assert isinstance(data["items"], list)
