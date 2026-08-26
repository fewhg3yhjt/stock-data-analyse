"""System health alerts built on the existing notification outbox."""

from __future__ import annotations

from datetime import datetime


def collect_alerts(status: dict, *, pending_threshold: int = 20) -> list[dict]:
    alerts = []
    for dataset in status.get("datasets", []):
        state = dataset.get("status")
        if state in {"stale", "critical", "failed", "empty"}:
            alert_type = "daily_critical" if dataset.get("dataset") == "daily" and state == "critical" else f"{dataset.get('dataset')}_{state}"
            alerts.append({"alert_type": alert_type, "dataset": dataset.get("dataset"),
                           "key": f"system:{alert_type}:{dataset.get('dataset')}:{datetime.now():%Y-%m-%d}",
                           "message": dataset.get("last_error") or f"{dataset.get('dataset')} 状态：{state}"})
    counts = status.get("notification_health", {})
    if counts.get("pending", 0) > pending_threshold:
        alerts.append({"alert_type": "outbox_backlog", "dataset": "outbox",
                       "key": f"system:outbox_backlog:outbox:{datetime.now():%Y-%m-%d}",
                       "message": f"通知待处理数量为 {counts['pending']}，超过阈值 {pending_threshold}"})
    return alerts


def enqueue_alerts(status: dict, *, pending_threshold: int = 20) -> list[int]:
    """Persist current system alerts as topic=system outbox messages."""
    from StockInvestmentTool.notifier.outbox import NotificationOutbox

    outbox = NotificationOutbox()
    ids = []
    for alert in collect_alerts(status, pending_threshold=pending_threshold):
        payload = {"topic": "system", "dedupe_key": alert["key"],
                   "title": "系统告警", "message": alert["message"],
                   "alert_type": alert["alert_type"], "created_at": datetime.now().isoformat(timespec="seconds")}
        # The existing outbox has no uniqueness constraint; avoid duplicate
        # notifications during repeated health checks within the same day.
        duplicate = any((item.get("payload") or {}).get("dedupe_key") == alert["key"]
                        for item in outbox.recent(200))
        if not duplicate:
            ids.append(outbox.enqueue("system", payload))
    return ids
