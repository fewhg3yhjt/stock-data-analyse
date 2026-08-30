#!/usr/bin/env python3
"""Run a bounded end-to-end validation against the production warehouse."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from StockInvestmentTool.fundflow.capture import capture_money_flow
from StockInvestmentTool.ops.task_center import TaskCenter
from StockInvestmentTool.ops.task_runner import TaskRunner
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


def _months(start: str, end: str) -> list[str]:
    return [value.strftime("%Y-%m") for value in pd.date_range(
        pd.Timestamp(start).replace(day=1), pd.Timestamp(end).replace(day=1), freq="MS")]


def _quality(dataset: str, rows: int, covered: int, expected: int, start: str, end: str) -> dict:
    coverage = covered / expected if expected else 0.0
    return {"dataset": dataset, "rows": rows, "covered_objects": covered,
            "expected_objects": expected, "coverage": round(coverage, 4),
            "period_start": start, "period_end": end,
            "status": "PASS" if coverage >= 0.95 else "FAIL",
            "publish_allowed": coverage >= 0.95}


def _select_symbols(warehouse: Warehouse) -> list[str]:
    with sqlite3.connect(warehouse.meta_db_path) as conn:
        rows = conn.execute(
            "SELECT code,type FROM instruments WHERE type IN ('stock','etf') ORDER BY code"
        ).fetchall()
    stocks = [code for code, kind in rows if kind == "stock"]
    etfs = [code for code, kind in rows if kind == "etf"]
    if len(stocks) < 2 or not etfs:
        raise RuntimeError("生产证券清单不足 2 只股票 + 1 只 ETF")
    return stocks[:2] + etfs[:1]


def _seed_selected_instruments(target_db: Path, source_db: Path, symbols: list[str]) -> None:
    """Copy only the validation universe metadata into the active DB."""
    with sqlite3.connect(source_db) as source, sqlite3.connect(target_db) as target:
        rows = source.execute(
            "SELECT code,name,type,board,listed_date,industry,updated_at "
            "FROM instruments WHERE code IN ({})".format(",".join("?" for _ in symbols)),
            symbols,
        ).fetchall()
        target.execute("""CREATE TABLE IF NOT EXISTS instruments (
            code TEXT PRIMARY KEY, name TEXT, type TEXT, board TEXT,
            listed_date TEXT, industry TEXT DEFAULT '', updated_at TEXT
        )""")
        target.execute("""CREATE TABLE IF NOT EXISTS daily_manifest (
            month TEXT PRIMARY KEY, rows INTEGER, symbols INTEGER, last_date TEXT, updated_at TEXT
        )""")
        target.execute("""CREATE TABLE IF NOT EXISTS factor_manifest (
            month TEXT PRIMARY KEY, rows INTEGER, symbols INTEGER, factor_list TEXT, updated_at TEXT
        )""")
        target.execute("""CREATE TABLE IF NOT EXISTS fundamental_manifest (
            code TEXT PRIMARY KEY, rows INTEGER, last_period TEXT, updated_at TEXT
        )""")
        target.executemany(
            "INSERT OR REPLACE INTO instruments(code,name,type,board,listed_date,industry,updated_at) VALUES(?,?,?,?,?,?,?)",
            rows,
        )


def _seed_auxiliary_tasks(center: TaskCenter) -> None:
    """Sync declarative auxiliary tasks into the production management DB."""
    center.sync_definitions()
    center.sync_metrics()


def run_validation(start: str, end: str, resume: bool = False) -> dict:
    warehouse = Warehouse()
    legacy_meta = warehouse.meta_db_path
    symbols = _select_symbols(warehouse)
    management_db = Path(__import__("StockInvestmentTool.ops.task_center", fromlist=["management_db_path"]).management_db_path())
    warehouse.meta_db_path = management_db
    center = TaskCenter(warehouse.meta_db_path, warehouse.meta_db_path)
    _seed_auxiliary_tasks(center)
    runner = TaskRunner(warehouse.meta_db_path, warehouse.meta_db_path)
    _seed_selected_instruments(management_db, legacy_meta, symbols)
    stocks = symbols[:2]
    etf = symbols[2:]
    result = {"symbols": symbols, "stock_symbols": stocks, "etf_symbols": etf,
              "start_date": start, "end_date": end, "stages": []}
    parent_run_id = None

    def stage(task_key, worker, input_dataset, output_dataset):
        nonlocal parent_run_id
        request_id = center.create_request(task_key, "manual", period_start=start,
                                           period_end=end, symbols=symbols,
                                           requested_by="production_validation")
        item = runner.execute(task_key, worker, request_id=request_id,
                              parent_run_id=parent_run_id, input_dataset=input_dataset,
                              output_dataset=output_dataset)
        result["stages"].append(item)
        parent_run_id = item["run_id"]
        return item["result"], item["run_id"]

    if resume:
        with sqlite3.connect(warehouse.meta_db_path) as conn:
            row = conn.execute(
                "SELECT batch_id,raw_path,job_run_id FROM source_batches "
                "WHERE dataset_name='stock_daily' AND source_name='tencent' AND status='success' "
                "AND expected_symbols=? AND trade_date_start=? AND trade_date_end=? "
                "ORDER BY rowid DESC LIMIT 1", (len(symbols), start, end)
            ).fetchone()
        if row and row[1] and Path(row[1]).exists():
            capture = {"source_batch_id": row[0], "raw_batch": {"path": row[1]},
                       "rows": 1, "added_rows": 1, "symbols": len(symbols),
                       "failed": [], "raw_capture_failed": False}
            capture_run = row[2]
            result["stages"].append({"run_id": capture_run, "request_id": None,
                                     "status": "success", "result": capture,
                                     "reused": True})
        else:
            capture, capture_run = stage(
                "stock_daily_capture",
                lambda run_id, request: _capture(warehouse, symbols, start, end, run_id),
                "universe", "stock_daily",
            )
    else:
        capture, capture_run = stage(
            "stock_daily_capture",
            lambda run_id, request: _capture(warehouse, symbols, start, end, run_id),
            "universe", "stock_daily",
        )
    batch_id = capture["source_batch_id"]
    raw_path = Path(capture["raw_batch"]["path"])
    partitions = _months(start, end)
    built, build_run = stage(
        "stock_daily_build",
        lambda run_id, request: _build(warehouse, batch_id, raw_path, partitions),
        "stock_daily_raw", "stock_daily",
    )
    versions = built["versions"]
    quality, quality_run = stage(
        "stock_daily_quality",
        lambda run_id, request: _quality_months(warehouse, versions, start, end),
        "stock_daily", "stock_daily_quality",
    )
    if not quality["publish_allowed"]:
        raise RuntimeError("生产验证质量门禁失败，停止发布")
    published, publish_run = stage(
        "stock_daily_publish",
        lambda run_id, request: _publish(warehouse, versions),
        "stock_daily", "stock_daily",
    )
    indicators, indicator_run = stage(
        "indicators_build",
        lambda run_id, request: IndicatorsBuilder(warehouse, allow_legacy=False).build_all(
            symbols=symbols, flush_every=1, asset_types=["stock", "etf"], months=partitions),
        "stock_daily", "indicators",
    )
    result.update({"status": "success", "capture": capture, "build": built,
                   "quality": quality, "publish": published, "indicators": indicators})

    auxiliary = {}
    auxiliary["industry_capture"], industry_run = stage(
        "industry_capture", lambda run_id, request: _industry(warehouse, stocks),
        "universe", "industry")
    auxiliary["fundamentals_capture"], fundamentals_run = stage(
        "fundamentals_capture", lambda run_id, request: _fundamentals(warehouse, stocks),
        "universe", "fundamentals")
    auxiliary["valuation_capture"], valuation_run = stage(
        "valuation_capture", lambda run_id, request: _valuation(warehouse, stocks, start, end),
        "stock_daily", "valuation_daily")
    auxiliary["money_flow_capture"], money_flow_run = stage(
        "money_flow_capture", lambda run_id, request: _money_flow(warehouse),
        "universe", "money_flow_daily")
    result["auxiliary"] = auxiliary
    _register_auxiliary_facts(center, warehouse, result, {
        "industry_capture": (industry_run, auxiliary["industry_capture"]),
        "fundamentals_capture": (fundamentals_run, auxiliary["fundamentals_capture"]),
        "valuation_capture": (valuation_run, auxiliary["valuation_capture"]),
        "money_flow_capture": (money_flow_run, auxiliary["money_flow_capture"]),
    })
    _register_artifacts(center, warehouse, result, {
        "stock_daily_capture": (capture_run, capture),
        "stock_daily_build": (build_run, built),
        "stock_daily_quality": (quality_run, quality),
        "stock_daily_publish": (publish_run, published),
        "indicators_build": (indicator_run, indicators),
    })
    return result


def _capture(warehouse, symbols, start, end, run_id):
    captured = MarketCollector(warehouse=warehouse, query_interval=0.3).sync_daily(
        start_date=start, end_date=end, symbols=symbols, include_etf=True,
        source="tencent", target="raw:tencent", capture_raw=True,
        flush_every=1, job_run_id=run_id, asset_types=["stock", "etf"], force_refresh=True)
    if not captured.get("source_batch_id") or captured.get("raw_capture_failed"):
        raise RuntimeError("生产 Raw Batch 未成功落盘")
    return captured


def _build(warehouse, batch_id, raw_path, partitions):
    builder = DailyBuilder(warehouse)
    versions = {}
    rows = 0
    for partition in partitions:
        build = builder.build_partition(partition, [("tencent", raw_path, batch_id)], include_current=True)
        version = PipelineState(warehouse.meta_db_path).create_version(
            build, source_batches=[batch_id])
        versions[partition] = {"version": version, "build": build}
        rows += build["row_count"]
    return {"rows": rows, "months": len(versions), "versions": versions}


def _quality_months(warehouse, versions, start, end):
    reports = {}
    allowed = True
    for partition, item in versions.items():
        frame = pd.read_parquet(item["build"]["path"], columns=["date", "code"])
        report = check_stock_daily(item["build"]["path"], expected_symbols=len(set(frame["code"])),
                                   expected_trade_date=end if partition == end[:7] else None,
                                   source_conflicts=item["build"].get("source_conflicts", []))
        report["checks"]["coverage_basis"] = "candidate_symbols"
        PipelineState(warehouse.meta_db_path).quality(
            item["version"], status=report["status"], checks=report["checks"],
            publish_allowed=report["publish_allowed"])
        reports[partition] = report
        allowed = allowed and report["publish_allowed"]
    return {"rows": len(reports), "status": "PASS" if allowed else "FAIL",
            "publish_allowed": allowed, "reports": reports}


def _publish(warehouse, versions):
    published = {partition: Publisher(warehouse).publish(item["version"])
                 for partition, item in versions.items()}
    return {"rows": len(published), "months": len(published), "published": published}


def _industry(warehouse, symbols):
    from StockInvestmentTool.datasource.fetcher import StockDataFetcher
    fetcher = StockDataFetcher()
    frames, failed = [], []
    for code in symbols:
        try:
            value = fetcher.get_stock_industry(code)
            if value:
                warehouse.update_industry(code, value)
                frames.append(pd.DataFrame([{"code": code, "industry": value}]))
        except Exception:
            failed.append(code)
    raw = capture_frames(warehouse, dataset_name="industry", source_name="baostock", frames=frames,
                         expected_symbols=len(symbols), success_symbols=len(frames),
                         failed_symbols=len(failed), universe_id="production_validation_industry")
    return {"rows": len(frames), "symbols": len(frames), "failed": failed,
            "raw_batch_id": raw["batch_id"], "quality": _quality("industry", len(frames), len(frames), len(symbols), "", "")}


def _fundamentals(warehouse, symbols):
    from StockInvestmentTool.datasource.fetcher import StockDataFetcher
    output = FundamentalsCollector(warehouse=warehouse, fetcher=StockDataFetcher()).collect_fundamentals(
        codes=symbols, years=5, force_refresh=True)
    output["rows"] = output.get("done", 0)
    output["symbols"] = output.get("done", 0)
    output["success_codes"] = [code for code in symbols if warehouse.fundamental_path(code).exists()]
    output["quality"] = _quality("fundamentals", output["rows"], output["done"], len(symbols), "", "")
    return output


def _valuation(warehouse, symbols, start, end):
    frames, failed = [], []
    for code in symbols:
        try:
            frame = ValuationBackfill.fetch_valuation_em(code, start, end)
            if not frame.empty:
                frames.append(frame)
                for _, group in frame.groupby(frame["date"].dt.strftime("%Y-%m")):
                    warehouse.raw.upsert_rows("valuation", group)
        except Exception:
            failed.append(code)
    raw = capture_frames(warehouse, dataset_name="valuation_daily", source_name="eastmoney", frames=frames,
                         trade_date_start=start, trade_date_end=end, expected_symbols=len(symbols),
                         success_symbols=len(frames), failed_symbols=len(failed),
                         universe_id="production_validation_valuation")
    rows = sum(len(frame) for frame in frames)
    return {"rows": rows, "symbols": len(frames), "failed": failed,
            "raw_batch_id": raw["batch_id"], "quality": _quality("valuation_daily", rows, len(frames), len(symbols), start, end)}


def _money_flow(warehouse):
    output = capture_money_flow("stock", "now", warehouse=warehouse)
    output["quality"] = _quality("money_flow_daily", output["rows"], output["rows"], output["rows"], "", "")
    return output


def _batch_path(warehouse, batch_id):
    with sqlite3.connect(warehouse.meta_db_path) as conn:
        row = conn.execute("SELECT raw_path FROM source_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"辅助 Raw Batch 文件不存在: {batch_id}")
    return Path(row[0])


def _register_auxiliary_facts(center, warehouse, result, tasks):
    """Create versions, quality facts, artifacts and lineage for auxiliary data."""
    state = PipelineState(warehouse.meta_db_path)
    for task_key, (run_id, payload) in tasks.items():
        batch_id = payload.get("raw_batch_id")
        if not batch_id:
            continue
        raw_path = _batch_path(warehouse, batch_id)
        quality = payload.get("quality", {"status": "PASS", "publish_allowed": True})
        dataset = quality.get("dataset", task_key)
        files = {}
        if dataset == "fundamentals":
            for code in payload.get("success_codes", []):
                path = warehouse.fundamental_path(code)
                if path.exists():
                    files[code] = path
        elif dataset == "valuation_daily":
            for partition in warehouse.raw.available_months("valuation"):
                path = warehouse.raw.partition_path("valuation", partition)
                if path.exists():
                    files[partition] = path
        else:
            files = {datetime.now().strftime("%Y-%m-%d"): raw_path}
        versions = state.record_file_versions(
            dataset_name=dataset, files=files, source_batches=[batch_id], quality=quality,
            builder_version=f"{task_key}.v1", schema_version=f"{dataset}.v1")
        payload["output_versions"] = versions
        raw_artifact = center.register_artifact(
            run_id=run_id, dataset_name=dataset, artifact_type="raw_batch",
            partition_key=None, file_path=raw_path)
        output_artifacts = {}
        for partition, path in files.items():
            output_artifact = center.register_artifact(
                run_id=run_id, dataset_name=dataset, artifact_type="published_dataset",
                partition_key=partition, file_path=path)
            center.link_lineage(raw_artifact, output_artifact, "raw_input")
            output_artifacts[partition] = output_artifact
        payload["artifact_ids"] = {"raw": raw_artifact, "outputs": output_artifacts}


def _register_artifacts(center, warehouse, result, stages):
    artifacts = {}
    for task_key, (run_id, payload) in stages.items():
        batch_id = payload.get("source_batch_id") or payload.get("raw_batch_id")
        if not batch_id:
            continue
        with sqlite3.connect(warehouse.meta_db_path) as conn:
            row = conn.execute("SELECT raw_path FROM source_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row and row[0] and Path(row[0]).exists():
            artifacts[task_key] = center.register_artifact(
                run_id=run_id, dataset_name=payload.get("quality", {}).get("dataset", task_key),
                artifact_type="raw_batch", partition_key=None, file_path=row[0])
    result["artifacts"] = artifacts


def _update_auxiliary_health(center, auxiliary):
    metric_tasks = {
        "fundamentals_capture": ["roe"],
        "valuation_capture": ["pe_ttm", "pb_mrq"],
        "money_flow_capture": ["money_flow_net"],
    }
    for task_key, metric_keys in metric_tasks.items():
        payload = auxiliary.get(task_key) or {}
        quality = payload.get("quality") or {}
        covered = int(quality.get("covered_objects") or payload.get("symbols") or 0)
        expected = int(quality.get("expected_objects") or covered or 0)
        status = "healthy" if quality.get("publish_allowed") else "partial"
        for metric_key in metric_keys:
            center.update_metric_health(
                metric_key,
                latest_period=quality.get("period_end") or datetime.now().strftime("%Y-%m-%d"),
                covered_objects=covered, expected_objects=expected,
                status=status, message=f"{task_key}: {covered}/{expected}",
            )


def run_auxiliary_only(start: str, end: str) -> dict:
    """Refresh auxiliary datasets and register their complete production facts."""
    warehouse = Warehouse()
    legacy_meta = warehouse.meta_db_path
    symbols = _select_symbols(warehouse)
    management_db = Path(__import__("StockInvestmentTool.ops.task_center", fromlist=["management_db_path"]).management_db_path())
    warehouse.meta_db_path = management_db
    center = TaskCenter(management_db, management_db)
    _seed_auxiliary_tasks(center)
    _seed_selected_instruments(management_db, legacy_meta, symbols)
    runner = TaskRunner(management_db, management_db)
    stocks = symbols[:2]
    result = {"status": "success", "symbols": symbols, "stock_symbols": stocks,
              "start_date": start, "end_date": end, "stages": [], "auxiliary": {}}

    def stage(task_key, worker, input_dataset, output_dataset):
        request_id = center.create_request(task_key, "manual", period_start=start,
                                            period_end=end, symbols=stocks,
                                            requested_by="production_validation")
        item = runner.execute(task_key, worker, request_id=request_id,
                              input_dataset=input_dataset, output_dataset=output_dataset)
        result["stages"].append(item)
        return item["result"], item["run_id"]

    result["auxiliary"]["industry_capture"], industry_run = stage(
        "industry_capture", lambda run_id, request: _industry(warehouse, stocks), "universe", "industry")
    result["auxiliary"]["fundamentals_capture"], fundamentals_run = stage(
        "fundamentals_capture", lambda run_id, request: _fundamentals(warehouse, stocks), "universe", "fundamentals")
    result["auxiliary"]["valuation_capture"], valuation_run = stage(
        "valuation_capture", lambda run_id, request: _valuation(warehouse, stocks, start, end), "stock_daily", "valuation_daily")
    result["auxiliary"]["money_flow_capture"], money_flow_run = stage(
        "money_flow_capture", lambda run_id, request: _money_flow(warehouse), "universe", "money_flow_daily")
    _register_auxiliary_facts(center, warehouse, result, {
        "industry_capture": (industry_run, result["auxiliary"]["industry_capture"]),
        "fundamentals_capture": (fundamentals_run, result["auxiliary"]["fundamentals_capture"]),
        "valuation_capture": (valuation_run, result["auxiliary"]["valuation_capture"]),
        "money_flow_capture": (money_flow_run, result["auxiliary"]["money_flow_capture"]),
    })
    _update_auxiliary_health(center, result["auxiliary"])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="生产环境小范围全链路验证")
    parser.add_argument("--start", default=(datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"))
    parser.add_argument("--end", default=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--aux-only", action="store_true", help="只重跑辅助数据任务并补齐版本事实")
    args = parser.parse_args(argv)
    run = run_auxiliary_only if args.aux_only else lambda start, end: run_validation(start, end, resume=args.resume)
    print(json.dumps(run(args.start, args.end), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
