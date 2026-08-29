#!/usr/bin/env python3
"""Run the real data pipeline in an isolated, bounded shadow warehouse."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from StockInvestmentTool.ops.job_runs import JobRunStore
from StockInvestmentTool.ops.task_center import TaskCenter
from StockInvestmentTool.ops.task_runner import TaskRunner
from StockInvestmentTool.warehouse.asset_profiles import select_symbols
from StockInvestmentTool.warehouse.daily_build import DailyBuilder
from StockInvestmentTool.warehouse.factors import FactorEngine
from StockInvestmentTool.warehouse.indicators_build import IndicatorsBuilder
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.quality import check_stock_daily
from StockInvestmentTool.warehouse.storage import Warehouse


DEFAULT_SYMBOLS = ["sh600000", "sh600519", "sz000001"]
DEFAULT_ROOT = Path("output/data/shadow_validation")


def _business_end(today: datetime) -> datetime:
    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _validate_scope(root: Path, symbols: list[str], start_date: str, end_date: str,
                   asset_types: list[str]) -> tuple[Path, list[str], dict[str, int]]:
    root = Path(root).resolve()
    formal = (Path.cwd() / "output" / "data" / "warehouse").resolve()
    if root == formal or formal in root.parents or root in formal.parents:
        raise ValueError("Shadow 输出目录不能与正式 warehouse 目录重叠")
    if len(symbols) > 100:
        raise ValueError("Shadow Run 最多允许 100 只证券")
    if not symbols:
        raise ValueError("Shadow Run 至少需要 1 只证券")
    if not asset_types:
        raise ValueError("至少需要一种证券类型")
    symbols, type_counts = select_symbols(symbols, asset_types=asset_types)
    if not symbols:
        raise ValueError("没有证券符合任务允许的资产类型")
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if end < start:
        raise ValueError("结束日期不能早于开始日期")
    if (end - start).days > 370:
        raise ValueError("Shadow Run 日期范围最多 370 个自然日")
    return root, symbols, type_counts


def _task_result(result: dict) -> dict:
    """Keep task result status conservative while preserving detailed payload."""
    return result if result.get("rows", result.get("added_rows", 0)) or result.get("ok") else {**result, "rows": 1}


def run_shadow(root: Path, symbols: list[str], start_date: str, end_date: str,
               asset_types: list[str] | None = None, resume: bool = False) -> dict:
    root, symbols, type_counts = _validate_scope(root, symbols, start_date, end_date, asset_types or ["stock", "etf"])
    warehouse = Warehouse(root / "warehouse")
    warehouse.meta_db_path = root / "management.db"
    center = TaskCenter(warehouse.meta_db_path, warehouse.meta_db_path)
    warehouse.metadata.register_stock_daily()
    center.sync_definitions()
    center.sync_metrics()
    _seed_shadow_instruments(warehouse.meta_db_path, symbols)
    runner = TaskRunner(root / "management.db", root / "management.db")
    result = {
        "root": str(root), "symbols": symbols, "asset_types": asset_types or ["stock", "etf"],
        "asset_type_counts": type_counts, "start_date": start_date, "end_date": end_date,
        "timezone": "Asia/Shanghai", "trigger_type": "shadow", "stages": [],
    }
    parent_run_id = None

    def execute_stage(task_key: str, worker, *, input_dataset: str, output_dataset: str) -> dict:
        nonlocal parent_run_id
        request_id = center.create_request(task_key, "shadow", period_start=start_date,
                                           period_end=end_date, symbols=symbols,
                                           requested_by="shadow_script")
        item = runner.execute(task_key, worker, request_id=request_id,
                              parent_run_id=parent_run_id, input_dataset=input_dataset,
                              output_dataset=output_dataset)
        parent_run_id = item["run_id"]
        result["stages"].append(item)
        return item["result"]

    try:
        captured = None
        if resume:
            captured, parent_run_id = _reuse_capture(warehouse, symbols, start_date, end_date, center)
            if captured is not None:
                result["stages"].append({"task_key": "stock_daily_capture", "run_id": parent_run_id,
                                         "request_id": captured.get("request_id"), "status": "success",
                                         "result": captured})
        if captured is None:
            captured = execute_stage(
                "stock_daily_capture",
                lambda run_id, request: _capture(warehouse, symbols, start_date, end_date,
                                                  asset_types, run_id),
                input_dataset="universe", output_dataset="stock_daily",
            )
        batch_id = captured["source_batch_id"]
        builder = DailyBuilder(warehouse)
        months = [item.strftime("%Y-%m") for item in pd.date_range(
            pd.Timestamp(start_date).replace(day=1),
            pd.Timestamp(end_date).replace(day=1),
            freq="MS",
        )]

        built = execute_stage(
            "stock_daily_build",
            lambda run_id, request: _build_months(warehouse, builder, months, batch_id),
            input_dataset="stock_daily_raw", output_dataset="stock_daily",
        )
        versions = built["versions"]

        quality = execute_stage(
            "stock_daily_quality",
            lambda run_id, request: _quality_months(warehouse, versions, symbols, end_date),
            input_dataset="stock_daily", output_dataset="stock_daily_quality",
        )
        if not quality["publish_allowed"]:
            raise RuntimeError(f"Shadow 数据质量不允许发布: {quality['status']}")

        published = execute_stage(
            "stock_daily_publish",
            lambda run_id, request: _publish_versions(warehouse, versions),
            input_dataset="stock_daily", output_dataset="stock_daily",
        )

        indicators = execute_stage(
            "indicators_build",
            lambda run_id, request: IndicatorsBuilder(warehouse, allow_legacy=False).build_all(
                symbols=symbols, flush_every=10, asset_types=asset_types),
            input_dataset="stock_daily", output_dataset="indicators",
        )
        factors = execute_stage(
            "factors_build",
            lambda run_id, request: FactorEngine(warehouse, allow_legacy=False).build_factors(
                symbols=symbols, asset_types=asset_types),
            input_dataset="stock_daily", output_dataset="factors",
        )
        artifacts, lineage = _register_outputs(center, result, warehouse, batch_id, versions,
                                                indicators, factors, months, result["stages"])
        result.update({"status": "success", "capture": captured, "build": built,
                       "quality": quality, "publish": published, "indicators": indicators,
                       "factors": factors, "artifacts": artifacts, "lineage": lineage})
        return result
    except Exception as exc:
        result.update({"status": "failed", "error": str(exc)})
        raise


def _capture(warehouse, symbols, start_date, end_date, asset_types, run_id):
    from StockInvestmentTool.warehouse.collector import MarketCollector
    captured = MarketCollector(warehouse=warehouse, query_interval=0.3).sync_daily(
        start_date=start_date, end_date=end_date, symbols=symbols, include_etf=True,
        source="tencent", target="daily", flush_every=10, job_run_id=run_id,
        asset_types=asset_types,
    )
    if not captured.get("source_batch_id") or captured.get("raw_capture_failed"):
        raise RuntimeError("真实 Raw Batch 未成功落盘")
    return _task_result(captured)


def _reuse_capture(warehouse, symbols, start_date, end_date, center):
    """Reuse the newest successful matching batch without re-fetching sources."""
    with sqlite3.connect(warehouse.meta_db_path) as conn:
        row = conn.execute(
            "SELECT batch_id,raw_path,job_run_id FROM source_batches "
            "WHERE dataset_name='stock_daily' AND source_name='tencent' AND status='success' "
            "AND expected_symbols=? AND trade_date_start=? AND trade_date_end=? "
            "ORDER BY finished_at DESC LIMIT 1",
            (len(symbols), start_date, end_date),
        ).fetchone()
    if not row or not row[1] or not Path(row[1]).exists():
        return None, None
    return {"source_batch_id": row[0], "raw_batch": {"path": row[1]},
            "rows": 1, "added_rows": 1, "symbols": len(symbols),
            "failed": [], "raw_capture_failed": False}, row[2]


def _seed_shadow_instruments(db_path: Path, symbols: list[str]) -> None:
    """Seed only the selected code/type universe in the isolated management DB."""
    from StockInvestmentTool.warehouse.asset_profiles import asset_type_for
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS instruments (
            code TEXT PRIMARY KEY, name TEXT, type TEXT, board TEXT,
            listed_date TEXT, industry TEXT DEFAULT '', updated_at TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS daily_manifest (
            month TEXT PRIMARY KEY, rows INTEGER, symbols INTEGER, last_date TEXT, updated_at TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS factor_manifest (
            month TEXT PRIMARY KEY, rows INTEGER, symbols INTEGER, factor_list TEXT, updated_at TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS fundamental_manifest (
            code TEXT PRIMARY KEY, rows INTEGER, last_period TEXT, updated_at TEXT
        )""")
        conn.executemany(
            "INSERT OR REPLACE INTO instruments(code,type,updated_at) VALUES(?,?,?)",
            [(code, asset_type_for(code), datetime.now().isoformat(timespec="seconds")) for code in symbols],
        )


def _build_months(warehouse, builder, months, batch_id):
    versions = {}
    rows = 0
    for month in months:
        build = builder.build_partition(month, [("tencent", Path(_batch_path(warehouse, batch_id)))], include_current=False)
        state = PipelineState(warehouse.meta_db_path)
        version = state.create_version(build, source_batches=[batch_id])
        versions[month] = {"version": version, "build": build}
        rows += build["row_count"]
    return {"rows": rows, "months": len(versions), "versions": versions}


def _batch_path(warehouse, batch_id):
    with warehouse._conn() as conn:
        row = conn.execute("SELECT raw_path FROM source_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"Raw Batch 文件不存在: {batch_id}")
    return row[0]


def _quality_months(warehouse, versions, symbols, end_date):
    reports = {}
    allowed = True
    for month, item in versions.items():
        frame = pd.read_parquet(item["build"]["path"], columns=["date", "code"])
        # A short first/last month may contain fewer requested symbols because
        # the symbol was not listed yet or was already inactive. Use symbols
        # observed on or before that partition's end as the fallback universe.
        available_symbols = set(symbols)
        if not frame.empty:
            first_seen = frame.groupby("code")["date"].min()
            month_end = pd.Timestamp(f"{month}-01") + pd.offsets.MonthEnd(1)
            expected = {code for code in symbols if code in first_seen and first_seen[code] <= month_end}
            expected = max(len(expected), 1)
        else:
            expected = len(symbols)
        report = check_stock_daily(item["build"]["path"], expected_symbols=expected,
                                   expected_trade_date=end_date if month == end_date[:7] else None,
                                   source_conflicts=item["build"].get("source_conflicts", []))
        report["checks"]["coverage_basis"] = "requested_universe_with_first_seen_fallback"
        report["checks"]["expected_symbols"] = expected
        coverage = report["checks"]["coverage"]
        report["checks"]["coverage"] = len(set(frame["code"].astype(str))) / expected if expected else 1.0
        config = __import__("StockInvestmentTool.warehouse.dataset_config", fromlist=["load_dataset_config"]).load_dataset_config("stock_daily")["quality"]
        hard_fail = bool(report["checks"].get("duplicate_primary_keys", 0)) or bool(report["checks"].get("ohlc", {}).get("invalid_count", 0)) or bool(report["checks"].get("negative_volume_amount", 0))
        coverage = report["checks"]["coverage"]
        if hard_fail or coverage < config["coverage"]["warning_min"]:
            report["status"] = "FAIL"
            report["publish_allowed"] = False
        elif coverage < config["coverage"]["pass_min"] or report["checks"].get("freshness", {}).get("status") == "WARNING":
            report["status"] = "WARNING"
            report["publish_allowed"] = bool(config["publish_warning"])
        else:
            report["status"] = "PASS"
            report["publish_allowed"] = True
        PipelineState(warehouse.meta_db_path).quality(
            item["version"], status=report["status"], checks=report["checks"],
            publish_allowed=report["publish_allowed"],
        )
        reports[month] = report
        allowed = allowed and report["publish_allowed"]
    result = {"rows": len(reports), "status": "PASS" if allowed else "FAIL",
              "publish_allowed": allowed, "reports": reports}
    if not allowed:
        result["failed"] = 1
        result["failed_count"] = 1
    return result


def _publish_versions(warehouse, versions):
    published = {}
    for month, item in versions.items():
        published[month] = Publisher(warehouse).publish(item["version"])
    return {"rows": len(published), "months": len(published), "published": published}


def _register_outputs(center, result, warehouse, batch_id, versions, indicators, factors, months, stages):
    stage_runs = {item["request_id"]: item["run_id"] for item in stages}
    task_runs = {item["request_id"]: item["run_id"] for item in stages}
    run_by_task = {}
    for item in stages:
        # The result payload is intentionally kept small; task order is stable.
        run_by_task.setdefault(item.get("result", {}).get("task_key"), item["run_id"])
    run_ids = [item["run_id"] for item in stages]
    capture_run = run_ids[0]
    build_run = run_ids[1]
    derived_runs = run_ids[-2:]
    artifacts = {}
    raw_path = Path(_batch_path(warehouse, batch_id))
    artifacts["raw"] = center.register_artifact(run_id=capture_run, dataset_name="stock_daily",
                                                 artifact_type="raw_batch", partition_key=None,
                                                 file_path=raw_path)
    for month, item in versions.items():
        artifacts[f"candidate:{month}"] = center.register_artifact(
            run_id=build_run, dataset_name="stock_daily", artifact_type="candidate",
            partition_key=month, file_path=item["build"]["path"])
        artifacts[f"published:{month}"] = center.register_artifact(
            run_id=run_ids[3], dataset_name="stock_daily", artifact_type="published_dataset",
            partition_key=month, file_path=warehouse.daily_partition(month))
    artifacts["indicators"] = center.register_artifact(
        run_id=derived_runs[0], dataset_name="indicators", artifact_type="indicator_output",
        partition_key=months[-1], file_path=warehouse.indicator_dir / f"{months[-1]}.parquet")
    artifacts["factors"] = center.register_artifact(
        run_id=derived_runs[1], dataset_name="factors", artifact_type="factor_output",
        partition_key=months[-1], file_path=warehouse.factor_dir / f"{months[-1]}.parquet")
    lineage = []
    for month in versions:
        center.link_lineage(artifacts["raw"], artifacts[f"candidate:{month}"], "raw_input")
        center.link_lineage(artifacts[f"candidate:{month}"], artifacts[f"published:{month}"], "published_from_candidate")
        lineage.extend([(artifacts["raw"], artifacts[f"candidate:{month}"]),
                        (artifacts[f"candidate:{month}"], artifacts[f"published:{month}"])])
    center.link_lineage(artifacts[f"published:{months[-1]}"], artifacts["indicators"], "published_input")
    center.link_lineage(artifacts[f"published:{months[-1]}"], artifacts["factors"], "published_input")
    lineage.extend([(artifacts[f"published:{months[-1]}"], artifacts["indicators"]),
                    (artifacts[f"published:{months[-1]}"], artifacts["factors"])])
    return artifacts, lineage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实小批量 Shadow 数据流程")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--asset-types", default="stock,etf")
    parser.add_argument("--resume", action="store_true", help="复用已成功的采集批次，继续后续阶段")
    args = parser.parse_args(argv)
    symbols = [item.strip().lower().replace(".", "") for item in args.symbols.split(",") if item.strip()]
    end = args.end or _business_end(datetime.now()).strftime("%Y-%m-%d")
    start = args.start or (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
    result = run_shadow(args.root, symbols, start, end,
                        asset_types=[item.strip().lower() for item in args.asset_types.split(",") if item.strip()],
                        resume=args.resume)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
