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
    LiveAdviceEvaluator,
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
        assert advice.strategy_decision_id == dec.decision_id
        assert advice.quantity_ratio == pytest.approx(0.2)
        assert advice.quantity is None
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
                         recipient="a@b.com", worker="w1")
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
            svc.repo.db.update("notification_deliveries", {"next_attempt_at": ""},
                               "delivery_id=?", (delivery.delivery_id,))
            svc.claim(delivery.delivery_id, worker="w1")
            ok = svc.deliver(delivery.delivery_id, channel, subject="s", body="b", recipient="a@b.com", worker="w1")
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

    def test_claim_is_atomic(self, svc):
        event = svc.create_event(event_type="RISK_ALERT", symbol="sh600908")
        delivery = svc.create_delivery(event, channel="email", recipient="a@b.com")
        assert svc.claim(delivery.delivery_id, "w1")
        assert not svc.claim(delivery.delivery_id, "w2")

    def test_delivery_requires_current_lease_owner(self, svc):
        event = svc.create_event(event_type="BUY_SIGNAL", symbol="sh600908")
        delivery = svc.create_delivery(event, channel="email", recipient="a@b.com")
        assert svc.claim(delivery.delivery_id, "w1")
        assert not svc.deliver(delivery.delivery_id, FakeChannel(True), subject="s", body="b",
                               recipient="a@b.com", worker="w2")
        assert svc.repo.db.fetchone(
            "SELECT status FROM notification_deliveries WHERE delivery_id=?", (delivery.delivery_id,)
        )["status"] == DELIVERY_PROCESSING

    def test_rule_delivery_respects_rule_and_is_idempotent(self, svc, tmp_path, monkeypatch):
        from StockInvestmentTool.biz import triggers

        path = tmp_path / "rules.yaml"
        triggers.save_triggers([{
            "id": "rule-risk", "name": "风险", "enabled": True,
            "kind": "notification_subscription", "event_type": "RISK_ALERT",
            "channel": "email", "priority": "instant",
        }], path)
        monkeypatch.setattr(triggers, "RULES_PATH", path)
        monkeypatch.setenv("EMAIL_TO", "alerts@example.com")
        event = svc.create_event(event_type="RISK_ALERT", symbol="sh600908")

        first = svc.create_rule_delivery(event, template="risk")
        second = svc.create_rule_delivery(event, template="risk")
        assert first.delivery_id == second.delivery_id
        assert svc.repo.db.fetchone("SELECT COUNT(*) AS n FROM notification_deliveries")["n"] == 1

    def test_rule_delivery_keeps_event_when_disabled(self, svc, tmp_path, monkeypatch):
        from StockInvestmentTool.biz import triggers

        path = tmp_path / "rules.yaml"
        triggers.save_triggers([{
            "id": "rule-risk", "name": "风险", "enabled": False,
            "kind": "notification_subscription", "event_type": "RISK_ALERT",
        }, {
            "id": "notification_trade_signals", "name": "买卖风险信号", "enabled": False,
            "kind": "notification_subscription", "event_types": ["RISK_ALERT"],
        }], path)
        monkeypatch.setattr(triggers, "RULES_PATH", path)
        monkeypatch.setenv("EMAIL_TO", "alerts@example.com")
        event = svc.create_event(event_type="RISK_ALERT", symbol="sh600908")

        assert svc.create_rule_delivery(event) is None
        assert svc.repo.db.fetchone("SELECT COUNT(*) AS n FROM notification_events")["n"] == 1
        assert svc.repo.db.fetchone("SELECT COUNT(*) AS n FROM notification_deliveries")["n"] == 0

    def test_failed_delivery_is_backed_off(self, svc):
        event = svc.create_event(event_type="RISK_ALERT", symbol="sh600908")
        delivery = svc.create_delivery(event, channel="email", recipient="a@b.com")
        assert svc.claim(delivery.delivery_id, "w1")
        assert not svc.deliver(delivery.delivery_id, FakeChannel(False), subject="s", body="b",
                               recipient="a@b.com", worker="w1")
        row = svc.repo.db.fetchone(
            "SELECT status,next_attempt_at,attempts FROM notification_deliveries WHERE delivery_id=?",
            (delivery.delivery_id,),
        )
        assert row["status"] == DELIVERY_PENDING
        assert row["attempts"] == 1
        assert row["next_attempt_at"]
        assert not svc.claim(delivery.delivery_id, "w2")

    def test_advice_lifecycle_and_delivery_updates(self, svc):
        from StockInvestmentTool.biz.models import StrategyContext
        from StockInvestmentTool.biz.strategy import StrategySpec, compile_strategy
        import pandas as pd

        df = pd.DataFrame({
            "date": pd.to_datetime(["2026-08-14"]), "code": ["sh600908"],
            "open": [11.0], "high": [12.0], "low": [10.0], "close": [11.8],
            "volume": [1000], "amount": [10000],
        })
        strategy = compile_strategy(StrategySpec(
            strategy_id="s1", version="1",
            entry_rules=[{"rule_id": "buy", "action": "BUY", "when": {
                "type": "comparison", "left": {"field": "close"},
                "operator": ">", "right": {"value": 10}}}],
            exit_rules=[{"rule_id": "sell", "action": "SELL_ALL", "when": {
                "type": "comparison", "left": {"field": "close"},
                "operator": "<", "right": {"value": 5}}}],
            position_sizing={"initial_ratio": 0.2},
        ), strategy_version_id="sv_test")
        context = StrategyContext(
            symbol="sh600908", evaluation_time="2026-08-14T15:00:00Z",
            data_as_of="2026-08-14", market_data=df, cash_available=10000,
        )
        decision, advice = LiveAdviceEvaluator(strategy, svc).evaluate(
            context, portfolio_id="pf1", position_cycle_id="pc1")
        assert decision.action == "BUY"
        assert advice.status == "generated"
        svc.transition_advice(advice.advice_id, "accepted")
        event = svc.create_event(event_type="BUY_SIGNAL", symbol=advice.symbol,
                                 advice_id=advice.advice_id, strategy_version_id="sv1",
                                 action=advice.action, data_as_of=advice.data_as_of,
                                 trigger_fingerprint="life")
        delivery = svc.create_delivery(event, "email", "test@example.com")
        svc.claim(delivery.delivery_id, "worker")
        assert svc.deliver(delivery.delivery_id, FakeChannel(True), subject="s", body="b",
                           recipient="test@example.com", worker="worker")
        assert svc.repo.db.fetchone(
            "SELECT status FROM advices WHERE advice_id=?", (advice.advice_id,)
        )["status"] == "accepted"


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
