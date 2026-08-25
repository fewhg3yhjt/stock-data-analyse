"""FR-3 通知编排器 API / 核心 测试。"""

from __future__ import annotations

import importlib

import pytest

from StockInvestmentTool.notifier.core import (
    Digest,
    MessageAggregator,
    NotificationFragment,
    TOPIC_FUNDFLOW,
    TOPIC_ORDERS,
)


@pytest.fixture
def client():
    from StockInvestmentTool.web.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s["admin"] = True
    return c


class TestAggregator:
    def test_dedup_same_topic_lines(self):
        agg = MessageAggregator()
        agg.add(NotificationFragment(TOPIC_ORDERS, "指令", ["- A 50%"]))
        agg.add(NotificationFragment(TOPIC_ORDERS, "指令", ["- A 50%", "- B 清仓"]))
        d = agg.digest()
        sec = d.sections[0]
        assert sec["lines"] == ["- A 50%", "- B 清仓"]

    def test_multiple_topics_sections(self):
        agg = MessageAggregator()
        agg.add(NotificationFragment(TOPIC_ORDERS, "指令", ["- A"]))
        agg.add(NotificationFragment(TOPIC_FUNDFLOW, "资金流", ["今日净额 100 亿"]))
        d = agg.digest()
        topics = [s["topic"] for s in d.sections]
        assert set(topics) == {TOPIC_ORDERS, TOPIC_FUNDFLOW}

    def test_empty_lines_no_section(self):
        agg = MessageAggregator()
        agg.add(NotificationFragment(TOPIC_ORDERS, "指令", []))
        assert agg.digest() is None

    def test_empty_digest_skip(self):
        d = Digest(sections=[{"topic": "x", "title": "t", "lines": ["", None]}])
        assert d.is_empty()


class TestTriggers:
    def test_default_config(self):
        from StockInvestmentTool.notifier import triggers
        rules = triggers.load_triggers()
        assert len(rules) >= 1
        for r in rules:
            assert "schedule" in r and "channel" in r and "conditions" in r

    def test_save_reload_roundtrip(self, tmp_path):
        from StockInvestmentTool.notifier import triggers
        rules = triggers.load_triggers()
        rules[0]["enabled"] = False
        p = tmp_path / "rules.yaml"
        triggers.save_triggers(rules, p)
        reloaded = triggers.load_triggers(p)
        assert reloaded[0]["enabled"] is False

    def test_enabled_triggers_filter(self, tmp_path):
        from StockInvestmentTool.notifier import triggers
        rules = [
            {"id": "a", "enabled": True, "schedule": {}, "channel": "feishu", "conditions": []},
            {"id": "b", "enabled": False, "schedule": {}, "channel": "feishu", "conditions": []},
        ]
        p = tmp_path / "rules.yaml"
        triggers.save_triggers(rules, p)
        enabled = triggers.enabled_triggers(p)
        assert [r["id"] for r in enabled] == ["a"]


class TestApi:
    def test_get_rules(self, client, monkeypatch):
        r = client.get("/api/notify/rules")
        assert r.status_code == 200
        d = r.get_json()
        assert d["status"] == "success"
        assert isinstance(d["rules"], list)
        assert "schedule_modes" in d

    def test_post_rules(self, client, monkeypatch, tmp_path):
        # 避免写真实 notifier/notify_rules.yaml，monkeypatch 保存路径
        from StockInvestmentTool.notifier import triggers
        target = tmp_path / "rules.yaml"
        monkeypatch.setattr(triggers, "RULES_PATH", target)
        rules = [{
            "id": "t1", "name": "测试", "enabled": True,
            "conditions": [{"type": "action", "params": {"advice_types": ["sell_all"]}}],
            "logic": "AND", "schedule": {"mode": "post_close", "time": "15:35"},
            "channel": "feishu", "priority": "batch",
        }]
        r = client.post("/api/notify/rules", json={"rules": rules})
        assert r.status_code == 200
        assert r.get_json()["count"] == 1
        assert target.exists()

    def test_mail_config(self, client):
        r = client.get("/api/notify/config")
        assert r.status_code == 200
        d = r.get_json()
        assert d["status"] == "success"
        assert "webhook" in d and "mail" in d
        # 不回显明文：webhook 字段只含 configured 布尔
        assert "feishu_configured" in d["webhook"]

    def test_test_send(self, client):
        # 可能因无 webhook 报错，但应返回 JSON 而非崩溃
        r = client.post("/api/notify/test", json={"channel": "feishu", "url": ""})
        assert r.status_code in (200, 400)
