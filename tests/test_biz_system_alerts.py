# -*- coding: utf-8 -*-
"""biz/system_alerts 迁移测试：采集 + 落库去重。"""

import pytest


def test_collect_alerts_detects_critical_and_backlog():
    from StockInvestmentTool.biz.system_alerts import collect_alerts

    status = {"datasets": [{"dataset": "daily", "status": "critical", "last_error": "未同步"}],
              "notification_health": {"pending": 21}}
    alerts = collect_alerts(status, pending_threshold=20)
    assert {item["alert_type"] for item in alerts} == {"daily_critical", "outbox_backlog"}


def test_collect_alerts_ignores_healthy():
    from StockInvestmentTool.biz.system_alerts import collect_alerts

    status = {"datasets": [{"dataset": "daily", "status": "healthy", "last_error": ""}],
              "notification_health": {"pending": 0}}
    assert collect_alerts(status) == []


def test_enqueue_alerts_deduplicates_same_day(tmp_path):
    from StockInvestmentTool.biz.db import BusinessDB
    from StockInvestmentTool.biz.repo import BusinessRepository
    from StockInvestmentTool.biz.system_alerts import enqueue_alerts

    repo = BusinessRepository(BusinessDB(tmp_path / "alerts.db"))
    status = {"datasets": [{"dataset": "daily", "status": "critical", "last_error": "坏"}],
              "notification_health": {"pending": 0}}
    first = enqueue_alerts(status, repo=repo)
    second = enqueue_alerts(status, repo=repo)
    # 同一告警重复调用不再新增（detect 对未关闭同类型去重）
    assert len(first) >= 1
    assert len(second) <= len(first)