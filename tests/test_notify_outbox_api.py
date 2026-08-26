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


def test_notify_outbox_filter_and_retry(client, tmp_path, monkeypatch):
    monkeypatch.setattr("StockInvestmentTool.config.Config.DATA_DIR", tmp_path)
    from StockInvestmentTool.notifier.outbox import NotificationOutbox
    item_id = NotificationOutbox().enqueue("system", {"topic": "system", "message": "test"})
    assert client.get("/api/notify/outbox?status=pending").status_code == 200
    response = client.post(f"/api/notify/outbox/{item_id}/retry")
    assert response.status_code == 200
    assert response.get_json()["state"] == "pending"
