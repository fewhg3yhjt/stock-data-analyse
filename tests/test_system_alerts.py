from __future__ import annotations

from StockInvestmentTool.notifier.system_alerts import collect_alerts, enqueue_alerts


def test_collect_alerts_and_deduplicated_enqueue(tmp_path, monkeypatch):
    status = {"datasets": [{"dataset": "daily", "status": "critical", "last_error": "未同步"}],
              "notification_health": {"pending": 21}}
    alerts = collect_alerts(status, pending_threshold=20)
    assert {item["alert_type"] for item in alerts} == {"daily_critical", "outbox_backlog"}
    monkeypatch.setattr("StockInvestmentTool.config.Config.DATA_DIR", tmp_path)
    assert len(enqueue_alerts(status)) == 2
    assert len(enqueue_alerts(status)) == 0
