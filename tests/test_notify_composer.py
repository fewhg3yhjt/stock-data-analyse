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