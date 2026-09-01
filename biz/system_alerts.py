# -*- coding: utf-8 -*-
"""系统健康告警迁移到 biz 体系。

替代旧 notifier/system_alerts.py + 旧 outbox 投递：
- 告警采集逻辑沿用旧 collect_alerts（读取数据新鲜度状态）
- 告警落库走 biz/reporting.SystemAlertService（system_alerts 表，自动去重）
- 产生通知事件走 biz/notification（可选投递）
"""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def collect_alerts(status: dict, *, pending_threshold: int = 20) -> list[dict]:
    """从数据新鲜度状态中收集告警条目（与旧实现一致）。"""
    alerts = []
    for dataset in status.get("datasets", []):
        state = dataset.get("status")
        if state in {"stale", "critical", "failed", "empty"}:
            alert_type = ("daily_critical" if dataset.get("dataset") == "daily"
                          and state == "critical" else f"{dataset.get('dataset')}_{state}")
            alerts.append({
                "alert_type": alert_type,
                "dataset": dataset.get("dataset"),
                "key": f"system:{alert_type}:{dataset.get('dataset')}:{datetime.now():%Y-%m-%d}",
                "message": dataset.get("last_error") or f"{dataset.get('dataset')} 状态：{state}",
            })
    counts = status.get("notification_health", {})
    if counts.get("pending", 0) > pending_threshold:
        alerts.append({
            "alert_type": "outbox_backlog", "dataset": "outbox",
            "key": f"system:outbox_backlog:outbox:{datetime.now():%Y-%m-%d}",
            "message": f"通知待处理数量为 {counts['pending']}，超过阈值 {pending_threshold}",
        })
    return alerts


def enqueue_alerts(status: dict, *, pending_threshold: int = 20, repo=None) -> list[str]:
    """落库当前系统告警（SystemAlertService.detect 自动按天去重）。

    返回本次新增/复用的告警 alert_id 列表。
    """
    from StockInvestmentTool.biz.reporting import SystemAlertService

    service = SystemAlertService(repo)
    ids = []
    for alert in collect_alerts(status, pending_threshold=pending_threshold):
        record = service.detect(
            alert["alert_type"],
            resource=alert["dataset"],
            failure_code="daily",
            message=alert["message"],
            details={"key": alert["key"]},
            priority=2,
        )
        ids.append(record["alert_id"])
    return ids