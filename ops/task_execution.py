"""Unified execution workers for configured data tasks."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from StockInvestmentTool.fundflow.capture import capture_money_flow
from StockInvestmentTool.ops.task_center import TaskCenter
from StockInvestmentTool.warehouse.backfill import ValuationBackfill
from StockInvestmentTool.warehouse.collector import MarketCollector
from StockInvestmentTool.warehouse.daily_build import DailyBuilder
from StockInvestmentTool.warehouse.fundamentals_collect import FundamentalsCollector
from StockInvestmentTool.warehouse.indicators_build import IndicatorsBuilder
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.quality import check_stock_daily
from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.storage import Warehouse


def _months(start: str | None, end: str | None) -> list[str]:
    end = end or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    start = start or (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
    return [value.strftime("%Y-%m") for value in pd.date_range(
        pd.Timestamp(start).replace(day=1), pd.Timestamp(end).replace(day=1), freq="MS")]


def _symbols(request: dict) -> list[str]:
    return [str(value).lower().replace(".", "") for value in request.get("symbols", [])]


def _batch(warehouse: Warehouse, batch_id: str) -> Path:
    with sqlite3.connect(warehouse.meta_db_path) as conn:
        row = conn.execute("SELECT raw_path FROM source_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"Raw Batch 不存在: {batch_id}")
    return Path(row[0])


def _capture(warehouse: Warehouse, request: dict, run_id: int) -> dict:
    symbols = _symbols(request)
    result = MarketCollector(warehouse=warehouse, query_interval=0.3).sync_daily(
        start_date=request.get("period_start"), end_date=request.get("period_end"), symbols=symbols,
        include_etf=True, source="tencent", target="raw:tencent", capture_raw=True,
        flush_every=10, job_run_id=run_id, asset_types=["stock", "etf"])
    if not result.get("source_batch_id") or result.get("raw_capture_failed"):
        raise RuntimeError("Raw Batch 未成功落盘")
    return result


def _build(warehouse: Warehouse, request: dict) -> dict:
    batch_id = request.get("input_batch_id")
    if not batch_id:
        with sqlite3.connect(warehouse.meta_db_path) as conn:
            row = conn.execute("SELECT batch_id FROM source_batches WHERE dataset_name='stock_daily' AND status='success' ORDER BY rowid DESC LIMIT 1").fetchone()
        if not row:
            raise RuntimeError("没有可用的 stock_daily Raw Batch")
        batch_id = row[0]
    path = _batch(warehouse, batch_id)
    versions = {}
    rows = 0
    for partition in _months(request.get("period_start"), request.get("period_end")):
        build = DailyBuilder(warehouse).build_partition(partition, [("tencent", path, batch_id)], include_current=True)
        version = PipelineState(warehouse.meta_db_path).create_version(build, source_batches=[batch_id])
        versions[partition] = {"version": version, "build": build}
        rows += build["row_count"]
    return {"rows": rows, "months": len(versions),
            "versions": versions,
            "output_versions": {partition: item["version"] for partition, item in versions.items()},
            "source_batch_id": batch_id}


def _quality(warehouse: Warehouse, request: dict) -> dict:
    versions = request.get("input_versions") or {}
    versions = {key: value.get("version") if isinstance(value, dict) else value
                for key, value in versions.items()}
    if not versions:
        with sqlite3.connect(warehouse.meta_db_path) as conn:
            rows = conn.execute(
                "SELECT partition_key,version_id FROM dataset_versions v "
                "WHERE dataset_name='stock_daily' AND publish_status='candidate' "
                "AND created_at=(SELECT MAX(v2.created_at) FROM dataset_versions v2 "
                "WHERE v2.dataset_name=v.dataset_name AND v2.partition_key=v.partition_key "
                "AND v2.publish_status='candidate')"
            ).fetchall()
        versions = {partition: version for partition, version in rows}
    # expected_symbols 基准：优先使用执行请求固化 symbols/symbol_count，
    # 禁止使用候选自身 symbol_count（避免覆盖率自证恒为 1.0）。
    expected_symbols = request.get("expected_symbols")
    if expected_symbols is None:
        symbols = _symbols(request)
        expected_symbols = len(symbols) if symbols else None
    reports = {}
    allowed = True
    statuses = []
    for partition, version in versions.items():
        with sqlite3.connect(warehouse.meta_db_path) as conn:
            row = conn.execute("SELECT candidate_path,symbol_count FROM dataset_versions WHERE version_id=?", (version,)).fetchone()
        if not row:
            raise RuntimeError(f"数据版本不存在: {version}")
        report = check_stock_daily(row[0], expected_symbols=expected_symbols,
                                   expected_trade_date=request.get("period_end") if partition == str(request.get("period_end", ""))[:7] else None,
                                   source_conflicts=[])
        PipelineState(warehouse.meta_db_path).quality(version, status=report["status"],
                                                       checks=report["checks"], publish_allowed=report["publish_allowed"])
        reports[partition] = report
        allowed = allowed and report["publish_allowed"]
        statuses.append(report["status"])
    # 真实聚合状态：任一 FAIL 则 FAIL；否则按最差质量结论，不得把 WARNING 伪装为 PASS。
    if "FAIL" in statuses:
        quality_status = "FAIL"
    elif "WARNING" in statuses:
        quality_status = "WARNING"
    else:
        quality_status = "PASS"
    return {"rows": len(reports), "ok": allowed,
            "status": quality_status, "publish_allowed": allowed,
            "failed_count": sum(1 for status in statuses if status == "FAIL"),
            "reports": reports,
            "input_versions": versions, "output_versions": versions if allowed else {}}


def _publish(warehouse: Warehouse, request: dict) -> dict:
    versions = request.get("input_versions") or {}
    versions = {key: value.get("version") if isinstance(value, dict) else value
                for key, value in versions.items()}
    if not versions:
        # 禁止隐式历史候选补位：Publish 没有明确版本时必须失败。
        raise RuntimeError("Publish 缺少明确的 input_versions，禁止隐式选择历史 Candidate")
    published = {}
    for partition, version in versions.items():
        with sqlite3.connect(warehouse.meta_db_path) as conn:
            row = conn.execute("SELECT quality_status,publish_status FROM dataset_versions WHERE version_id=?", (version,)).fetchone()
        if not row:
            raise RuntimeError(f"数据版本不存在: {version}")
        if row[0] not in ("PASS", "WARNING"):
            raise RuntimeError(f"版本 {version} 质量未通过，禁止发布 (quality={row[0]})")
        if row[1] != "candidate":
            raise RuntimeError(f"版本 {version} 不是 candidate，禁止发布 (publish_status={row[1]})")
        published[partition] = Publisher(warehouse).publish(version)
    return {"rows": len(published), "published": published,
            "ok": True,
            "input_versions": versions,
            "output_versions": {key: value.get("version_id", versions[key])
                                 for key, value in published.items()}}


def _auxiliary(warehouse: Warehouse, request: dict, task_key: str) -> dict:
    symbols = [code for code in _symbols(request) if code.startswith(("sh6", "sz0", "sz3", "bj4", "bj8"))]
    if task_key == "industry_capture":
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher
        fetcher = StockDataFetcher()
        frames = []
        for code in symbols:
            value = fetcher.get_stock_industry(code)
            if value:
                warehouse.update_industry(code, value)
                frames.append(pd.DataFrame([{"code": code, "industry": value}]))
        raw = capture_frames(warehouse, dataset_name="industry", source_name="baostock", frames=frames,
                             expected_symbols=len(symbols), success_symbols=len(frames), universe_id="industry_task")
        return {"rows": len(frames), "symbols": len(frames), "raw_batch_id": raw["batch_id"]}
    if task_key == "fundamentals_capture":
        output = FundamentalsCollector(warehouse=warehouse).collect_fundamentals(codes=symbols, years=5)
        output["rows"] = output.get("done", 0)
        output["symbols"] = output.get("done", 0)
        output["success_codes"] = [code for code in symbols if warehouse.fundamental_path(code).exists()]
        return output
    if task_key == "valuation_capture":
        frames = []
        for code in symbols:
            frame = ValuationBackfill.fetch_valuation_em(code, request.get("period_start"), request.get("period_end"))
            if not frame.empty:
                frames.append(frame)
                for _, group in frame.groupby(frame["date"].dt.strftime("%Y-%m")):
                    warehouse.raw.upsert_rows("valuation", group)
        raw = capture_frames(warehouse, dataset_name="valuation_daily", source_name="eastmoney", frames=frames,
                             trade_date_start=request.get("period_start"), trade_date_end=request.get("period_end"),
                             expected_symbols=len(symbols), success_symbols=len(frames), universe_id="valuation_task")
        return {"rows": sum(len(frame) for frame in frames), "symbols": len(frames), "raw_batch_id": raw["batch_id"]}
    output = capture_money_flow("stock", "now", warehouse=warehouse)
    return output


def worker(task_key: str, warehouse: Warehouse, request: dict, run_id: int) -> dict:
    if task_key == "stock_daily_capture":
        return _capture(warehouse, request, run_id)
    if task_key == "stock_daily_build":
        return _build(warehouse, request)
    if task_key == "stock_daily_quality":
        return _quality(warehouse, request)
    if task_key == "stock_daily_publish":
        return _publish(warehouse, request)
    if task_key == "indicators_build":
        return IndicatorsBuilder(warehouse, allow_legacy=False).build_all(
            symbols=_symbols(request), asset_types=["stock", "etf"], months=_months(request.get("period_start"), request.get("period_end")),
            partition_versions=request.get("input_versions") or None)
    if task_key in {"industry_capture", "fundamentals_capture", "valuation_capture", "money_flow_capture"}:
        return _auxiliary(warehouse, request, task_key)
    raise ValueError(f"未注册的任务: {task_key}")


def execute_task(db_path: Path, task_key: str, payload: dict) -> dict:
    center = TaskCenter(db_path, db_path)
    task = center.task(task_key)
    if task is None:
        raise ValueError(f"任务不存在: {task_key}")
    symbols = payload.get("symbols") or []
    if not symbols:
        config = json.loads(task["config_versions"][0]["config"]) if task.get("config_versions") else {}
        asset_types = set((config.get("scope") or {}).get("asset_types") or [])
        with sqlite3.connect(db_path) as conn:
            has_instruments = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='instruments'"
            ).fetchone()
            rows = conn.execute("SELECT code,type FROM instruments ORDER BY code").fetchall() if has_instruments else []
        symbols = [code for code, kind in rows if not asset_types or kind in asset_types]
    if not symbols:
        raise ValueError(f"任务 {task_key} 没有可执行的证券范围")
    payload = {**payload, "symbols": symbols}
    request_id = center.create_request(task_key, payload.get("trigger_type", "manual"),
                                       period_start=payload.get("period_start"), period_end=payload.get("period_end"),
                                       symbols=symbols, requested_by=payload.get("requested_by", "admin"),
                                       input_versions=payload.get("input_versions") or {})
    from StockInvestmentTool.ops.task_runner import TaskRunner
    warehouse = Warehouse()
    warehouse.meta_db_path = db_path
    runner = TaskRunner(db_path, db_path)
    return runner.execute(task_key, lambda run_id, request: worker(task_key, warehouse, {**request, **payload}, run_id),
                          request_id=request_id, input_dataset=payload.get("input_dataset", ""),
                          output_dataset=payload.get("output_dataset", ""),
                          parent_run_id=payload.get("parent_run_id"))


def execute_pipeline(db_path: Path, task_keys: list[str], payload: dict | None = None) -> dict:
    """Run configured task keys in order and pass each result to its child."""
    if not task_keys:
        raise ValueError("流水线至少需要一个任务")
    payload = dict(payload or {})
    runs = []
    parent_run_id = None
    input_versions = payload.get("input_versions") or {}
    for task_key in task_keys:
        current = {**payload, "input_versions": input_versions, "parent_run_id": parent_run_id}
        item = execute_task(db_path, task_key, current)
        runs.append(item)
        result = item.get("result") or {}
        parent_run_id = item.get("run_id")
        output_versions = result.get("output_versions") or result.get("versions") or {}
        if output_versions:
            input_versions = output_versions
        if result.get("source_batch_id"):
            payload["input_batch_id"] = result["source_batch_id"]
        if item.get("status") not in {"success", "partial_success", "skipped"}:
            break
    return {"status": runs[-1].get("status", "failed"), "runs": runs,
            "request_ids": [item.get("request_id") for item in runs]}
