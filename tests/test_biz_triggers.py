# -*- coding: utf-8 -*-
"""biz/triggers 迁移测试：规则存储 + 条件评估 + 主流程（数据源用桩）。"""

import pytest


def test_save_and_load_triggers_roundtrip(tmp_path):
    from StockInvestmentTool.biz.triggers import load_triggers, save_triggers

    rules = [
        {"id": "r1", "name": "测试规则", "enabled": True,
         "conditions": [{"type": "action", "params": {"advice_types": ["sell_all"]}}],
         "logic": "AND", "schedule": {"mode": "intraday", "interval_minutes": 5},
         "channel": "email", "priority": "instant"},
    ]
    path = tmp_path / "rules.yaml"
    save_triggers(rules, path)
    loaded = load_triggers(path)
    assert loaded[0]["id"] == "r1"
    assert loaded[0]["conditions"][0]["type"] == "action"


def test_load_triggers_default_when_missing(tmp_path):
    from StockInvestmentTool.biz.triggers import load_triggers

    loaded = load_triggers(tmp_path / "missing.yaml")
    assert len(loaded) == 3
    ids = {r["id"] for r in loaded}
    assert {"actionable_intraday", "post_close_summary", "price_threshold"} <= ids


def test_enabled_triggers_filters_disabled(tmp_path):
    from StockInvestmentTool.biz.triggers import enabled_triggers, save_triggers

    rules = [
        {"id": "a", "enabled": True},
        {"id": "b", "enabled": False},
    ]
    path = tmp_path / "rules.yaml"
    save_triggers(rules, path)
    assert [r["id"] for r in enabled_triggers(path)] == ["a"]


def test_email_recipients_from_env(monkeypatch):
    from StockInvestmentTool.biz.triggers import email_recipients

    monkeypatch.setenv("EMAIL_TO", "a@x.com, b@y.com")
    assert email_recipients() == ["a@x.com", "b@y.com"]


def test_mail_config_status_does_not_leak_password(tmp_path, monkeypatch):
    from StockInvestmentTool.biz.triggers import mail_config_status

    env = tmp_path / ".env"
    env.write_text("EMAIL_USER=u\nEMAIL_PASSWORD=secret\nEMAIL_TO=a@x.com\n")
    monkeypatch.setattr("StockInvestmentTool.biz.triggers._env_file", lambda: env)
    status = mail_config_status()
    assert status["email_configured"] is True
    assert "secret" not in str(status)


def test_evaluate_condition_action():
    from StockInvestmentTool.biz.triggers import _evaluate_condition

    data = {"positions": [{"advice": {"advice_type": "sell_all"}}]}
    cond = {"type": "action", "params": {"advice_types": ["sell_all", "partial_sell"]}}
    assert _evaluate_condition(cond, data) is True
    cond2 = {"type": "action", "params": {"advice_types": ["buy_more"]}}
    assert _evaluate_condition(cond2, data) is False


def test_evaluate_condition_action_empty_wanted():
    from StockInvestmentTool.biz.triggers import _evaluate_condition

    data = {"positions": [{"advice": {"advice_type": "hold"}}]}
    cond = {"type": "action", "params": {}}
    assert _evaluate_condition(cond, data) is True


def test_evaluate_condition_unknown_type():
    from StockInvestmentTool.biz.triggers import _evaluate_condition

    assert _evaluate_condition({"type": "unknown", "params": {}}, {"positions": []}) is False


def test_run_trigger_rule_skipped_when_condition_not_met(tmp_path, monkeypatch):
    """条件未满足时不产生事件。"""
    from StockInvestmentTool.biz.triggers import run_trigger_rule

    # 数据源桩：war_room 无持仓，action 条件不命中
    import StockInvestmentTool.biz.triggers as mod

    class FakeMgr:
        def refresh_all(self):
            pass

    monkeypatch.setattr("StockInvestmentTool.portfolio.manager.PortfolioManager", FakeMgr)
    monkeypatch.setattr(
        "StockInvestmentTool.portfolio.dashboard.DashboardService",
        lambda mgr: type("DS", (), {"war_room": lambda self: {"positions": []}})()
    )
    rule = {"id": "r", "name": "t", "enabled": True,
            "conditions": [{"type": "action", "params": {"advice_types": ["sell_all"]}}],
            "logic": "AND", "schedule": {"mode": "intraday", "interval_minutes": 5},
            "channel": "email", "priority": "batch"}
    result = run_trigger_rule(rule)
    assert result["skipped"] is True


def test_run_trigger_rule_creates_event(tmp_path, monkeypatch):
    """条件命中且有收件人时创建事件 + 投递。"""
    from StockInvestmentTool.biz.triggers import run_trigger_rule

    import StockInvestmentTool.biz.triggers as mod

    class FakeMgr:
        def refresh_all(self):
            pass

    war_room = {
        "positions": [{"stock_name": "长江电力", "stock_code": "sh600908",
                       "advice_label": "卖出", "advice": {"advice_type": "sell_all", "reason": "破位"},
                       "current_price": 10.0, "unrealized_pnl_pct": "5.0"}],
        "summary": {"total_pnl_pct": "5.0"},
    }
    monkeypatch.setattr("StockInvestmentTool.portfolio.manager.PortfolioManager", FakeMgr)
    monkeypatch.setattr(
        "StockInvestmentTool.portfolio.dashboard.DashboardService",
        lambda mgr: type("DS", (), {"war_room": lambda self: war_room})()
    )
    monkeypatch.setattr(mod, "_watch_price_lines", lambda *a, **k: [])
    monkeypatch.setattr(mod, "_indicator_lines", lambda *a, **k: [])
    monkeypatch.setenv("EMAIL_TO", "t@example.com")

    # 用临时 DB，隔离真实仓库
    from StockInvestmentTool.biz.db import BusinessDB
    from StockInvestmentTool.biz.repo import BusinessRepository
    repo = BusinessRepository(BusinessDB(tmp_path / "trig.db"))

    rule = {"id": "r1", "name": "盘中卖出", "enabled": True,
            "conditions": [{"type": "action", "params": {"advice_types": ["sell_all"]}}],
            "logic": "AND", "schedule": {"mode": "post_close", "time": "15:35"},
            "channel": "email", "priority": "batch"}
    result = run_trigger_rule(rule, repo=repo)
    assert result["ok"] is True
    assert result["event_id"]
    assert result["delivery_id"]

    event = repo.db.fetchone(
        "SELECT * FROM notification_events WHERE event_id=?", (result["event_id"],))
    assert event["event_type"] == "TRIGGER"
    assert event["subject_id"] == "r1"

    delivery = repo.db.fetchone(
        "SELECT * FROM notification_deliveries WHERE delivery_id=?", (result["delivery_id"],))
    assert delivery["status"] == "pending"
    assert delivery["recipient"] == "t@example.com"


def test_run_trigger_rule_intraday_daily_limit(tmp_path, monkeypatch):
    """盘中配额到顶后跳过（不重复推送）。"""
    from StockInvestmentTool.biz.triggers import run_trigger_rule

    import StockInvestmentTool.biz.triggers as mod

    class FakeMgr:
        def refresh_all(self):
            pass

    war_room = {
        "positions": [{"stock_name": "x", "stock_code": "sh600908", "advice_label": "卖出",
                       "advice": {"advice_type": "sell_all"}, "current_price": 1, "unrealized_pnl_pct": "1"}],
        "summary": {"total_pnl_pct": "1"},
    }
    monkeypatch.setattr("StockInvestmentTool.portfolio.manager.PortfolioManager", FakeMgr)
    monkeypatch.setattr(
        "StockInvestmentTool.portfolio.dashboard.DashboardService",
        lambda mgr: type("DS", (), {"war_room": lambda self: war_room})()
    )
    monkeypatch.setattr(mod, "_watch_price_lines", lambda *a, **k: [])
    monkeypatch.setattr(mod, "_indicator_lines", lambda *a, **k: [])
    monkeypatch.setenv("EMAIL_TO", "t@example.com")
    monkeypatch.setenv("INTRADAY_NOTIFY_DAILY_LIMIT", "1")

    from StockInvestmentTool.biz.db import BusinessDB
    from StockInvestmentTool.biz.repo import BusinessRepository
    repo = BusinessRepository(BusinessDB(tmp_path / "trig2.db"))

    # 第一次：命中，进入配额 state（count=1 仅当提交；batch 不提交配额，这里用 intraday 验证）
    # 直接置状态：count 已达上限 → 应跳过
    monkeypatch.setattr(mod, "_load_state", lambda: {f"trigger:r:{_today()}": 1})
    rule = {"id": "r", "name": "盘中", "enabled": True,
            "conditions": [{"type": "action", "params": {"advice_types": ["sell_all"]}}],
            "logic": "AND", "schedule": {"mode": "intraday", "interval_minutes": 5},
            "channel": "email", "priority": "instant"}
    result = run_trigger_rule(rule, repo=repo)
    assert result["skipped"] is True
    assert result["reason"] == "daily_limit"


def _today():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")