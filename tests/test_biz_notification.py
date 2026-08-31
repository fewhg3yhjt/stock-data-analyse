# -*- coding: utf-8 -*-
"""biz 包单元测试：Advice / Notification / Outbox。"""

import pytest

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.models import StrategyDecision, new_id
from StockInvestmentTool.biz.notification import (
    DELIVERY_DEAD,
    DELIVERY_PENDING,
    DELIVERY_PROCESSING,
    DELIVERY_SENT,
    EmailChannel,
    NotificationService,
    register_channel,
)
from StockInvestmentTool.biz.repo import BusinessRepository


@pytest.fixture
def svc(tmp_path):
    return NotificationService(BusinessRepository(BusinessDB(tmp_path / "nt.db")))


class TestNotificationService:
    def test_create_event_dedupes(self, svc):
        e1 = svc.create_event(event_type="BUY_SIGNAL", symbol="sh600908",
                              strategy_version_id="sv1", data_as_of="2026-08-14",
                              action="BUY", trigger_fingerprint="f1")
        e2 = svc.create_event(event_type="BUY_SIGNAL", symbol="sh600908",
                              strategy_version_id="sv1", data_as_of="2026-08-14",
                              action="BUY", trigger_fingerprint="f1")
        assert e1.event_id == e2.event_id  # 同一出现指纹去重
        # 不同指纹产生新事件
        e3 = svc.create_event(event_type="BUY_SIGNAL", symbol="sh600908",
                              strategy_version_id="sv1", data_as_of="2026-08-14",
                              action="BUY", trigger_fingerprint="f2")
        assert e3.event_id != e1.event_id
        assert len(svc.list_events()) == 2

    def test_advice_from_decision(self, svc):
        dec = StrategyDecision(
            decision_id=new_id("dec"), strategy_id="s1", strategy_version="1",
            strategy_version_id="sv1", symbol="sh600908",
            decision_time="2026-08-14T15:00:00Z", data_as_of="2026-08-14",
            action="BUY", quantity_ratio=0.2, price=11.8,
            decision_trace={"triggered_rules": [{"rule_id": "pullback"}]},
            reason="趋势回踩",
        )
        advice = svc.advice_from_decision(dec, portfolio_id="pf1", position_cycle_id="pc1")
        assert advice.action == "BUY"
        assert advice.triggered_rules == ["pullback"]
        row = svc.repo.db.fetchone("SELECT * FROM advices WHERE advice_id=?", (advice.advice_id,))
        assert row["symbol"] == "sh600908"

    def test_delivery_flow_success(self, svc):
        event = svc.create_event(event_type="SELL_SIGNAL", symbol="sh601211")
        delivery = svc.create_delivery(event, channel="email", recipient="a@b.com")
        ok = svc.claim(delivery.delivery_id, worker="w1")
        assert ok
        row = svc.repo.db.fetchone("SELECT * FROM notification_deliveries WHERE delivery_id=?",
                                   (delivery.delivery_id,))
        assert row["status"] == DELIVERY_PROCESSING
        ok = svc.deliver(delivery.delivery_id, FakeChannel(True), subject="s", body="b",
                         recipient="a@b.com")
        assert ok
        row = svc.repo.db.fetchone("SELECT * FROM notification_deliveries WHERE delivery_id=?",
                                   (delivery.delivery_id,))
        assert row["status"] == DELIVERY_SENT

    def test_delivery_fail_retries_then_dead(self, svc):
        event = svc.create_event(event_type="RISK_ALERT", symbol="sh601211")
        delivery = svc.create_delivery(event, channel="email", recipient="a@b.com")
        channel = FakeChannel(False)
        from StockInvestmentTool.biz.notification import MAX_ATTEMPTS
        last_status = None
        for _ in range(MAX_ATTEMPTS):
            svc.claim(delivery.delivery_id, worker="w1")
            ok = svc.deliver(delivery.delivery_id, channel, subject="s", body="b", recipient="a@b.com")
            last_status = svc.repo.db.fetchone(
                "SELECT status FROM notification_deliveries WHERE delivery_id=?",
                (delivery.delivery_id,))["status"]
            if ok:
                break
        assert last_status == DELIVERY_DEAD
        assert not ok

    def test_lease_not_takeover_when_active(self, svc):
        event = svc.create_event(event_type="SYSTEM_ALERT")
        delivery = svc.create_delivery(event, channel="email", recipient="a@b.com")
        svc.claim(delivery.delivery_id, worker="w1")
        assert not svc.claim(delivery.delivery_id, worker="w2")  # 租约未过期


class TestEmailChannel:
    def test_config_preserves_password(self):
        ch = EmailChannel("smtp.test.com", 587, "a@b.com", username="u", password="secret")
        assert "secret" not in repr(ch)
        assert "secret" not in str(ch)


class FakeChannel:
    def __init__(self, ok: bool):
        self.ok = ok

    def send(self, subject, body, recipient):
        return self.ok


register_channel("fake", lambda: FakeChannel(True))