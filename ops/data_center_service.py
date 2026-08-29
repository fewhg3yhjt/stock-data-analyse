"""Page-oriented data-center aggregation over definitions and health facts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from StockInvestmentTool.ops.task_center import TaskCenter
from StockInvestmentTool.ops.task_center_service import TaskCenterService
from StockInvestmentTool.warehouse.asset_profiles import applicability


class DataCenterService:
    CORE_KEYS = ("stock_daily", "close", "indicators", "ma20", "factors", "pe_ttm", "roe", "money_flow_net")

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.center = TaskCenter(self.db_path, self.db_path)
        self.tasks = TaskCenterService(self.db_path)

    def overview(self) -> dict:
        assets = self.assets()
        core = [item for key in self.CORE_KEYS for item in assets if item["asset_key"] == key]
        attention = [item for item in assets if item["health"]["status"] in ("partial", "stale", "critical")]
        counts = {"total": len(assets), "healthy": 0, "attention": 0, "critical": 0, "unknown": 0}
        for item in assets:
            status = item["health"]["status"]
            if status == "healthy": counts["healthy"] += 1
            elif status in ("partial", "stale"): counts["attention"] += 1
            elif status == "critical": counts["critical"] += 1
            else: counts["unknown"] += 1
        task_items = self.tasks.tasks()
        pipeline_keys = ("stock_daily_capture", "stock_daily_build", "stock_daily_quality", "stock_daily_publish", "indicators_build", "factors_build")
        return {"counts": counts, "core_assets": core, "attention": attention,
                "task_summary": self.tasks.overview(),
                "pipeline": [item for key in pipeline_keys for item in task_items if item["task_key"] == key]}

    def assets(self, category=None, status=None, date=None) -> list[dict]:
        records = self.center.data_assets(category=category, status=status, date=date)
        return [self._asset_dto(record) for record in records]

    def asset(self, asset_key: str) -> dict | None:
        item = next((record for record in self.assets() if record["asset_key"] == asset_key), None)
        if item is None:
            return None
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            sources = [dict(row) for row in conn.execute("SELECT * FROM dataset_sources WHERE dataset_name=? ORDER BY priority", (asset_key,)).fetchall()]
            consumers = [dict(row) for row in conn.execute("SELECT * FROM dataset_consumers WHERE dataset_name=?", (asset_key,)).fetchall()]
        item["sources"] = sources
        item["consumers"] = consumers
        item["applicability"] = {kind: applicability(kind, "metrics", asset_key) for kind in ("stock", "etf", "index")} if item["kind"] == "metric" else {}
        item["producer_task_detail"] = self.tasks.task(item["producer_task"]) if item.get("producer_task") else None
        return item

    @staticmethod
    def _asset_dto(record):
        by_type = record.get("coverage_by_type") or "{}"
        try: by_type = json.loads(by_type) if isinstance(by_type, str) else by_type
        except (TypeError, ValueError): by_type = {}
        return {"asset_key": record["metric_key"], "display_name": record["display_name"],
                "kind": "dataset" if record.get("category") == "数据集" else "metric",
                "category": record.get("category") or "数据集", "definition": record.get("definition"),
                "unit": record.get("unit"), "producer_task": record.get("producer_task"),
                "editable": bool(record.get("editable")), "builtin": bool(record.get("builtin")),
                "latest_actual_date": record.get("latest_period"), "expected_date": record.get("expected_period"),
                "health": {"status": record.get("health_status") or "unknown", "message": record.get("message") or "",
                           "last_success_at": record.get("last_success_at")},
                "coverage": {"covered": record.get("covered_objects"), "expected": record.get("expected_objects"),
                             "ratio": record.get("coverage"), "by_asset_type": by_type},
                "updated_at": record.get("updated_at")}
