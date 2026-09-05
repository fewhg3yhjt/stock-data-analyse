"""Page-oriented data-center aggregation over definitions and health facts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from StockInvestmentTool.ops.task_center import TaskCenter
from StockInvestmentTool.ops.task_center_service import TaskCenterService
from StockInvestmentTool.warehouse.asset_profiles import applicability


class DataCenterService:
    CORE_KEYS = ("stock_daily", "close", "indicators", "ma20", "pe_ttm", "roe", "money_flow_net")

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
        pipeline_keys = ("stock_daily_capture", "stock_daily_build", "stock_daily_quality", "stock_daily_publish", "indicators_build", "industry_features_build", "industry_rotation_build", "industry_capture", "fundamentals_capture", "valuation_capture", "money_flow_capture")
        task_summary = self.tasks.overview()
        return {"counts": counts, "core_assets": core, "attention": attention,
                "task_summary": task_summary,
                "recent_tasks": sorted(task_items, key=lambda item: (item.get("running", False), item.get("latest_run") is not None), reverse=True)[:8],
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
            dataset_name = asset_key if item["kind"] == "dataset" else self._metric_dataset(asset_key, conn)
            sources = [dict(row) for row in conn.execute("SELECT * FROM dataset_sources WHERE dataset_name=? ORDER BY priority", (dataset_name,)).fetchall()]
            consumers = [dict(row) for row in conn.execute("SELECT * FROM dataset_consumers WHERE dataset_name=?", (dataset_name,)).fetchall()]
            dataset_versions = [dict(row) for row in conn.execute("SELECT version_id,partition_key,publish_status,quality_status,published_at,source_batches,input_versions FROM dataset_versions WHERE dataset_name=? ORDER BY created_at DESC LIMIT 20", (dataset_name,)).fetchall()]
        item["sources"] = sources
        item["consumers"] = consumers
        item["applicability"] = {kind: applicability(kind, "metrics", asset_key) for kind in ("stock", "etf", "index")} if item["kind"] == "metric" else {}
        item["producer_task_detail"] = self.tasks.task(item["producer_task"]) if item.get("producer_task") else None
        item["input_datasets"] = self._metric_inputs(asset_key, item.get("producer_task"))
        item["business_impact"] = self._business_impact(asset_key, item["kind"], dataset_name)
        item["result_files"] = self._result_files(item["kind"], asset_key, dataset_name)
        item["versions"] = dataset_versions
        return item

    @staticmethod
    def _metric_dataset(metric_key: str, conn) -> str:
        row = conn.execute("SELECT producer_task FROM metric_definitions WHERE metric_key=?", (metric_key,)).fetchone()
        task = row[0] if row else ""
        if task == "indicators_build":
            return "indicators"
        return "stock_daily"

    def _metric_inputs(self, metric_key: str, producer_task: str | None) -> list[dict]:
        if not producer_task:
            return []
        task = self.tasks.task(producer_task)
        return [{"asset_key": name, "display_name": name} for name in ((task or {}).get("config") or {}).get("task", {}).get("input_datasets", [])]

    def _business_impact(self, asset_key: str, kind: str, dataset_name: str) -> list[dict]:
        impacts = []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT consumer_name,purpose,blocked_actions,required_quality "
                "FROM dataset_consumers WHERE dataset_name=?", (dataset_name,)
            ).fetchall()
        impacts.extend({"consumer": row["consumer_name"], "purpose": row["purpose"],
                        "blocked_actions": row["blocked_actions"],
                        "required_quality": row["required_quality"]} for row in rows)
        if kind == "metric" and not impacts:
            impacts.append({"consumer": "关联任务下游", "purpose": "依赖该指标",
                            "blocked_actions": "指标结果过期时暂停相关业务"})
        if not impacts and dataset_name == "stock_daily":
            impacts.append({"consumer": "技术指标", "purpose": "生成派生结果",
                            "blocked_actions": "输入滞后时结果标记过期"})
        return impacts

    def _result_files(self, kind: str, asset_key: str, dataset_name: str) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if kind == "dataset":
                rows = conn.execute("SELECT * FROM dataset_partitions WHERE dataset_name=? ORDER BY partition_key DESC LIMIT 20", (dataset_name,)).fetchall()
                return [dict(row) for row in rows]
            rows = conn.execute("SELECT * FROM dataset_versions WHERE dataset_name=? ORDER BY created_at DESC LIMIT 20", (dataset_name,)).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _asset_dto(record):
        by_type = record.get("coverage_by_type") or "{}"
        try: by_type = json.loads(by_type) if isinstance(by_type, str) else by_type
        except (TypeError, ValueError): by_type = {}
        health_status = record.get("health_status")
        if not health_status and record.get("metric_key"):
            health_status = "healthy" if record.get("latest_period") else "unknown"
        return {"asset_key": record["metric_key"], "display_name": record["display_name"],
                "kind": "dataset" if record.get("category") == "数据集" else "metric",
                "category": record.get("category") or "数据集", "definition": record.get("definition"),
                "unit": record.get("unit"), "producer_task": record.get("producer_task"),
                "editable": bool(record.get("editable")), "builtin": bool(record.get("builtin")),
                "latest_actual_date": record.get("latest_period"), "expected_date": record.get("expected_period"),
                 "health": {"status": health_status or "unknown", "message": record.get("message") or "",
                           "last_success_at": record.get("last_success_at")},
                "coverage": {"covered": record.get("covered_objects"), "expected": record.get("expected_objects"),
                             "ratio": record.get("coverage"), "by_asset_type": by_type},
                "updated_at": record.get("updated_at"),
                "latest_actual_date": record.get("latest_period"),
                "expected_date": record.get("expected_period")}
