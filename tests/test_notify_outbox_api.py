"""Notification delivery ledger API tests（迁移到 biz 投递表）。"""

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
    assert "event_counts" in data


def test_notify_outbox_filter_and_retry(client, tmp_path, monkeypatch):
    from StockInvestmentTool.biz.db import BusinessDB
    from StockInvestmentTool.biz.repo import BusinessRepository
    from StockInvestmentTool.biz.notification import NotificationService

    monkeypatch.setattr("StockInvestmentTool.config.Config.DATA_DIR", tmp_path)
    repo = BusinessRepository(BusinessDB())
    service = NotificationService(repo)
    event = service.create_event(event_type="SYSTEM_ALERT", symbol="",
                                 payload={"subject": "测试", "text": "测试"})
    delivery = service.create_delivery(event, "email", "test@example.com", template="test")

    assert client.get("/api/notify/outbox?status=pending").status_code == 200
    response = client.post(f"/api/notify/outbox/{delivery.delivery_id}/retry")
    assert response.status_code == 200
    assert response.get_json()["state"] == "pending"
