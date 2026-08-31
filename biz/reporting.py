# -*- coding: utf-8 -*-
"""结构化日报和系统告警基础服务。"""

from __future__ import annotations

from dataclasses import dataclass, field

from StockInvestmentTool.biz.db import dumps_json, loads_json, now_utc
from StockInvestmentTool.biz.models import new_id


@dataclass
class DailyReport:
    report_id: str
    report_date: str
    data_as_of: str | None = None
    market_snapshot: dict = field(default_factory=dict)
    observation_snapshot: dict = field(default_factory=dict)
    portfolio_snapshot: dict = field(default_factory=dict)
    advice_ids: list = field(default_factory=list)
    sections: dict = field(default_factory=dict)
    status: str = "draft"


class ReportingService:
    def __init__(self, repo=None):
        from StockInvestmentTool.biz.repo import BusinessRepository
        self.repo = repo or BusinessRepository()

    def save_daily_report(self, report: DailyReport) -> DailyReport:
        self.repo.db.upsert("daily_reports", {
            "report_id": report.report_id,
            "report_date": report.report_date,
            "generated_at": now_utc(),
            "data_as_of": report.data_as_of or "",
            "market_snapshot_json": dumps_json(report.market_snapshot),
            "observation_snapshot_json": dumps_json(report.observation_snapshot),
            "portfolio_snapshot_json": dumps_json(report.portfolio_snapshot),
            "advice_ids_json": dumps_json(report.advice_ids),
            "sections_json": dumps_json(report.sections),
            "status": report.status,
            "created_at": now_utc(),
        }, "report_id")
        return report

    def get_daily_report(self, report_date: str) -> dict | None:
        row = self.repo.db.fetchone(
            "SELECT * FROM daily_reports WHERE report_date=? ORDER BY generated_at DESC LIMIT 1",
            (report_date,),
        )
        if not row:
            return None
        result = dict(row)
        for column, key in (
            ("market_snapshot_json", "market_snapshot"),
            ("observation_snapshot_json", "observation_snapshot"),
            ("portfolio_snapshot_json", "portfolio_snapshot"),
            ("advice_ids_json", "advice_ids"),
            ("sections_json", "sections"),
        ):
            result[key] = loads_json(result.pop(column))
        return result

    def create_report(self, report_date: str, *, data_as_of: str | None = None,
                      market_snapshot: dict | None = None,
                      observation_snapshot: dict | None = None,
                      portfolio_snapshot: dict | None = None,
                      advice_ids: list | None = None,
                      sections: dict | None = None) -> DailyReport:
        return self.save_daily_report(DailyReport(
            report_id=new_id("report"), report_date=report_date,
            data_as_of=data_as_of, market_snapshot=market_snapshot or {},
            observation_snapshot=observation_snapshot or {},
            portfolio_snapshot=portfolio_snapshot or {},
            advice_ids=advice_ids or [], sections=sections or {}, status="generated",
        ))


class SystemAlertService:
    """系统告警生命周期：detected → notified → acknowledged → recovered → closed。"""

    def __init__(self, repo=None):
        from StockInvestmentTool.biz.repo import BusinessRepository
        self.repo = repo or BusinessRepository()

    def detect(self, alert_type: str, *, resource: str = "", failure_code: str = "",
               message: str = "", details: dict | None = None, priority: int = 0) -> dict:
        row = self.repo.db.fetchone(
            "SELECT * FROM system_alerts WHERE alert_type=? AND resource=? "
            "AND failure_code=? AND status NOT IN ('closed') LIMIT 1",
            (alert_type, resource, failure_code),
        )
        if row:
            return dict(row)
        ts = now_utc()
        alert_id = new_id("alert")
        self.repo.db.insert("system_alerts", {
            "alert_id": alert_id, "alert_type": alert_type, "resource": resource,
            "failure_code": failure_code, "status": "detected", "priority": priority,
            "message": message, "details_json": dumps_json(details or {}),
            "detected_at": ts, "notified_at": "", "acknowledged_at": "",
            "recovered_at": "", "closed_at": "", "created_at": ts, "updated_at": ts,
        })
        return self.get(alert_id)

    def transition(self, alert_id: str, status: str) -> dict:
        allowed = {
            "detected": {"notified", "acknowledged", "recovered", "closed"},
            "notified": {"acknowledged", "recovered", "closed"},
            "acknowledged": {"recovered", "closed"},
            "recovered": {"closed"},
            "closed": set(),
        }
        current = self.repo.db.fetchone(
            "SELECT status FROM system_alerts WHERE alert_id=?", (alert_id,)
        )
        if not current:
            raise KeyError(f"unknown alert: {alert_id}")
        if status not in allowed.get(current["status"], set()):
            raise ValueError(f"invalid alert transition: {current['status']} -> {status}")
        ts = now_utc()
        column = {"notified": "notified_at", "acknowledged": "acknowledged_at",
                  "recovered": "recovered_at", "closed": "closed_at"}.get(status)
        updates = {"status": status, "updated_at": ts}
        if column:
            updates[column] = ts
        self.repo.db.update("system_alerts", updates, "alert_id=?", (alert_id,))
        return self.get(alert_id)

    def get(self, alert_id: str) -> dict | None:
        row = self.repo.db.fetchone("SELECT * FROM system_alerts WHERE alert_id=?", (alert_id,))
        if not row:
            return None
        result = dict(row)
        result["details"] = loads_json(result.pop("details_json"))
        return result

    def list_active(self) -> list[dict]:
        rows = self.repo.db.fetchall(
            "SELECT * FROM system_alerts WHERE status!='closed' ORDER BY priority DESC, detected_at DESC"
        )
        return [self.get(row["alert_id"]) for row in rows]
