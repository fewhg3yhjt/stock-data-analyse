# -*- coding: utf-8 -*-
"""biz/daily_digest 迁移测试：消息构造纯函数与 digest 主流程。"""

import pandas as pd
import pytest

from StockInvestmentTool.biz.daily_digest import (
    DigestRules,
    build_daily_messages,
    build_fundflow_messages,
    build_orders_messages,
    build_price_messages,
)


def test_build_price_messages_breakout():
    rules = DigestRules(watchlist=[
        {"code": "sh600908", "name": "长江电力", "price_above": 10.0},
    ])
    quotes = [{"code": "sh600908", "name": "长江电力", "price": 10.5, "change_pct": 1.0}]
    msgs = build_price_messages(rules, quotes)
    assert len(msgs) == 1
    assert "突破 10.0" in msgs[0]


def test_build_price_messages_below_and_no_hit():
    rules = DigestRules(watchlist=[
        {"code": "sh600908", "price_below": 9.0},
        {"code": "sz000001", "price_below": 5.0},
    ])
    quotes = [
        {"code": "sh600908", "price": 8.8, "change_pct": 0},
        {"code": "sz000001", "price": 10.0, "change_pct": 0},
    ]
    msgs = build_price_messages(rules, quotes)
    assert len(msgs) == 1
    assert "跌破 9.0" in msgs[0]


def test_build_fundflow_disabled_returns_empty():
    rules = DigestRules(fundflow={"enable": False})
    assert build_fundflow_messages(rules, {}, pd.DataFrame(), pd.DataFrame(), None) == []


def test_build_daily_messages():
    rules = DigestRules(daily={"enable": True})
    overview = {"上涨": 3000, "下跌": 2000, "全市场净额合计(亿)": 120, "价涨钱走(背离)": 5}
    top = pd.DataFrame([{"name": "银行", "net": 15.0}, {"name": "券商", "net": 10.0}])
    bottom = pd.DataFrame([{"name": "地产", "net": -8.0}])
    lines = build_daily_messages(rules, overview, top, bottom)
    assert len(lines) == 1
    assert "银行(15.0亿)" in lines[0]
    assert "上涨 3000" in lines[0]


def test_build_orders_messages():
    data = {
        "data_date": "2026-09-01",
        "summary": {"position_count": 1, "total_pnl_pct": "5.0"},
        "risk_status": "normal",
        "positions": [{
            "stock_name": "长江电力", "stock_code": "sh600908",
            "advice_label": "持有", "advice": {"reason": "测试"},
            "current_price": 11.0, "unrealized_pnl_pct": "5.0",
        }],
    }
    lines = build_orders_messages(data)
    assert len(lines) == 1
    assert "长江电力" in lines[0]
    assert "测试" in lines[0]


def test_build_daily_digest_emits_event(tmp_path, monkeypatch):
    """有持仓指令片段时生成 DAILY_REPORT 事件（持仓数据源用桩替换）。"""
    from StockInvestmentTool.biz.db import BusinessDB
    from StockInvestmentTool.biz.repo import BusinessRepository

    repo = BusinessRepository(BusinessDB(tmp_path / "digest.db"))

    # 用桩替换数据获取，避免真实网络/旧持仓依赖
    import StockInvestmentTool.biz.daily_digest as mod
    from StockInvestmentTool.biz import triggers
    rules_path = tmp_path / "notify_rules.yaml"
    triggers.save_triggers([{
        "id": "notification_daily_report", "name": "每日盘后汇总", "enabled": True,
        "kind": "notification_subscription", "event_type": "DAILY_REPORT",
        "conditions": [], "schedule": {}, "channel": "email", "priority": "batch",
    }], rules_path)
    monkeypatch.setattr(triggers, "RULES_PATH", rules_path)

    monkeypatch.setattr(mod, "_gather_digest_data", lambda rules: [
        mod.DigestSection(mod.TOPIC_ORDERS, "今日持仓指令", ["- 长江电力(sh600908): 持有 现价11.0 盈亏5.0%"]),
    ])
    monkeypatch.setenv("EMAIL_TO", "test@example.com")

    result = mod.build_daily_digest(repo)
    assert result.get("sections") == 1
    assert result.get("event_id")
    assert result.get("delivery_id")

    event = repo.db.fetchone(
        "SELECT * FROM notification_events WHERE event_id=?", (result["event_id"],)
    )
    assert event["event_type"] == "DAILY_REPORT"
    assert event["dedupe_key"]

    delivery = repo.db.fetchone(
        "SELECT * FROM notification_deliveries WHERE delivery_id=?", (result["delivery_id"],)
    )
    assert delivery["status"] == "pending"


def test_send_pending_deliveries_no_email_config(tmp_path):
    """无 EMAIL 配置时投递跳过，不报错。"""
    from StockInvestmentTool.biz.db import BusinessDB
    from StockInvestmentTool.biz.repo import BusinessRepository
    from StockInvestmentTool.biz.daily_digest import send_pending_deliveries

    repo = BusinessRepository(BusinessDB(tmp_path / "digest2.db"))
    result = send_pending_deliveries(repo)
    assert result["delivered"] == 0
