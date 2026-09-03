"""FR-3 通知编排器 API 测试（迁移到 biz 触发器/通知体系）。"""

from __future__ import annotations

import pytest


@pytest.fixture
def client():
    from StockInvestmentTool.web.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s["admin"] = True
    return c


class TestTriggers:
    def test_default_config(self):
        from StockInvestmentTool.biz.triggers import load_triggers
        rules = load_triggers()
        assert len(rules) >= 1
        for r in rules:
            assert "schedule" in r and "channel" in r and "conditions" in r

    def test_save_reload_roundtrip(self, tmp_path):
        from StockInvestmentTool.biz.triggers import load_triggers, save_triggers
        rules = load_triggers()
        rules[0]["enabled"] = False
        p = tmp_path / "rules.yaml"
        save_triggers(rules, p)
        reloaded = load_triggers(p)
        assert reloaded[0]["enabled"] is False

    def test_enabled_triggers_filter(self, tmp_path):
        from StockInvestmentTool.biz.triggers import enabled_triggers, save_triggers
        rules = [
            {"id": "a", "enabled": True, "schedule": {}, "channel": "email", "conditions": []},
            {"id": "b", "enabled": False, "schedule": {}, "channel": "email", "conditions": []},
        ]
        p = tmp_path / "rules.yaml"
        save_triggers(rules, p)
        enabled = enabled_triggers(p)
        assert [r["id"] for r in enabled] == ["a"]

    def test_notification_subscriptions_are_visible_but_not_scheduled(self):
        from StockInvestmentTool.biz.triggers import enabled_triggers, load_triggers

        all_rules = load_triggers()
        ids = {rule["id"] for rule in enabled_triggers()}
        assert "notification_task_failed" in {rule["id"] for rule in all_rules}
        assert "notification_task_failed" not in ids


class TestApi:
    def test_get_rules(self, client, monkeypatch):
        r = client.get("/api/notify/rules")
        assert r.status_code == 200
        d = r.get_json()
        assert d["status"] == "success"
        assert isinstance(d["rules"], list)
        assert "schedule_modes" in d

    def test_post_rules(self, client, monkeypatch, tmp_path):
        # 避免写真实 biz/notify_rules.yaml，monkeypatch 保存路径
        from StockInvestmentTool.biz import triggers
        target = tmp_path / "rules.yaml"
        monkeypatch.setattr(triggers, "RULES_PATH", target)
        rules = [{
            "id": "t1", "name": "测试", "enabled": True,
            "conditions": [{"type": "action", "params": {"advice_types": ["sell_all"]}}],
            "logic": "AND", "schedule": {"mode": "post_close", "time": "15:35"},
            "channel": "email", "priority": "batch",
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
        # 可能因无邮件配置报错，但应返回 JSON 而非崩溃
        r = client.post("/api/notify/test", json={"channel": "email", "to": ""})
        assert r.status_code in (200, 400)

    def test_bulk_rule_actions(self, client, monkeypatch, tmp_path):
        from StockInvestmentTool.biz import triggers

        target = tmp_path / "rules.yaml"
        monkeypatch.setattr(triggers, "RULES_PATH", target)
        triggers.save_triggers([
            {"id": "r1", "name": "一", "enabled": True, "conditions": [], "schedule": {}, "channel": "email"},
            {"id": "r2", "name": "二", "enabled": True, "conditions": [], "schedule": {}, "channel": "email"},
        ], target)
        response = client.post("/api/notify/rules/bulk", json={"action": "disable", "ids": ["r1", "r2"]})
        assert response.status_code == 200
        assert all(not rule["enabled"] for rule in triggers.load_triggers(target) if rule["id"] in {"r1", "r2"})

    def test_bulk_delete_keeps_system_subscription(self, client, monkeypatch, tmp_path):
        from StockInvestmentTool.biz import triggers

        target = tmp_path / "rules.yaml"
        monkeypatch.setattr(triggers, "RULES_PATH", target)
        triggers.save_triggers([{
            "id": "notification_task_failed", "name": "任务失败", "enabled": True,
            "kind": "notification_subscription", "event_type": "TASK_FAILED",
            "conditions": [], "schedule": {}, "channel": "email",
        }, {"id": "custom", "name": "自定义", "enabled": True,
              "conditions": [], "schedule": {}, "channel": "email"}], target)
        response = client.post("/api/notify/rules/bulk", json={"action": "delete", "ids": ["notification_task_failed", "custom"]})
        assert response.status_code == 200
        ids = {rule["id"] for rule in triggers.load_triggers(target)}
        assert "notification_task_failed" in ids
        assert "custom" not in ids
