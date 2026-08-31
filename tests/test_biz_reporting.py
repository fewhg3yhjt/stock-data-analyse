# -*- coding: utf-8 -*-

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.reporting import DailyReport, ReportingService, SystemAlertService


def test_daily_report_round_trip(tmp_path):
    repo = BusinessRepository(BusinessDB(tmp_path / "report.db"))
    service = ReportingService(repo)
    report = service.create_report(
        "2026-08-31", data_as_of="2026-08-28",
        portfolio_snapshot={"equity": 100000}, advice_ids=["adv1"],
    )
    loaded = service.get_daily_report("2026-08-31")
    assert loaded["report_id"] == report.report_id
    assert loaded["portfolio_snapshot"]["equity"] == 100000
    assert loaded["advice_ids"] == ["adv1"]


def test_system_alert_lifecycle_and_deduplication(tmp_path):
    repo = BusinessRepository(BusinessDB(tmp_path / "alert.db"))
    service = SystemAlertService(repo)
    first = service.detect("TASK_FAILED", resource="screen.run", failure_code="E1", message="failed")
    duplicate = service.detect("TASK_FAILED", resource="screen.run", failure_code="E1", message="again")
    assert duplicate["alert_id"] == first["alert_id"]
    service.transition(first["alert_id"], "notified")
    service.transition(first["alert_id"], "acknowledged")
    service.transition(first["alert_id"], "recovered")
    final = service.transition(first["alert_id"], "closed")
    assert final["status"] == "closed"
    assert service.list_active() == []
