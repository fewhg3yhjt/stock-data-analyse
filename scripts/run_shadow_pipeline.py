#!/usr/bin/env python3
"""Run a bounded real-source pipeline in an isolated shadow warehouse."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from StockInvestmentTool.ops.job_runs import JobRunStore
from StockInvestmentTool.ops.task_center import TaskCenter
from StockInvestmentTool.ops.task_runner import TaskRunner
from StockInvestmentTool.warehouse.daily_build import DailyBuilder
from StockInvestmentTool.warehouse.factors import FactorEngine
from StockInvestmentTool.warehouse.indicators_build import IndicatorsBuilder
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.quality import check_stock_daily
from StockInvestmentTool.warehouse.storage import Warehouse


DEFAULT_SYMBOLS = ["sh600000", "sh600001", "sz000001"]
DEFAULT_ROOT = Path("output/data/shadow_validation")


def _business_end(today: datetime) -> datetime:
    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def run_shadow(root: Path, symbols: list[str], start_date: str, end_date: str) -> dict:
    root = Path(root).resolve()
    forbidden = (Path.cwd() / "output" / "data" / "warehouse").resolve()
    if root == forbidden or forbidden in root.parents or root in forbidden.parents:
        raise ValueError("Shadow 输出目录不能与正式 warehouse 目录重叠")
    if len(symbols) > 5:
        raise ValueError("Shadow Run 最多允许 5 只证券")
    if not symbols:
        raise ValueError("Shadow Run 至少需要 1 只证券")
    if (end := datetime.strptime(end_date, "%Y-%m-%d")) < datetime.strptime(start_date, "%Y-%m-%d"):
        raise ValueError("结束日期不能早于开始日期")
    if (end - datetime.strptime(start_date, "%Y-%m-%d")).days > 10:
        raise ValueError("Shadow Run 日期范围最多 10 个自然日")

    warehouse = Warehouse(root)
    warehouse.metadata.register_stock_daily()
    metadata = TaskCenter(root / "job_runs.db", warehouse.meta_db_path)
    metadata.sync_definitions()
    metadata.sync_metrics()
    job_store = JobRunStore(root / "job_runs.db")
    runner = TaskRunner(root / "job_runs.db", warehouse.meta_db_path)
    request_id = runner.center.create_request("stock_daily_capture", "shadow",
                                              period_start=start_date, period_end=end_date,
                                              symbols=symbols, requested_by="shadow_script")
    request = runner.center.request(request_id)
    run_id = runner.jobs.start("stock_daily_capture", run_date=end_date, display_name="腾讯日线采集",
                               input_dataset="universe", output_dataset="stock_daily",
                               request_id=request_id, config_version=request["config_version"],
                               trigger_type="shadow", period_start=start_date, period_end=end_date)
    runner.center.update_request(request_id, "running")
    result = {"root": str(root), "symbols": symbols, "start_date": start_date, "end_date": end_date,
              "run_id": run_id, "request_id": request_id, "trigger_type": "shadow", "timezone": "Asia/Shanghai"}
    try:
        runner.center.event(run_id, "开始真实腾讯采集", phase="数据采集", event_type="start")
        job_store.update_progress(run_id, phase="数据采集", progress=5, total=len(symbols))
        collector = __import__("StockInvestmentTool.warehouse.collector", fromlist=["MarketCollector"]).MarketCollector(
            warehouse=warehouse, query_interval=0.3
        )
        captured = collector.sync_daily(start_date=start_date, end_date=end_date, symbols=symbols,
                                        include_etf=True, source="tencent", target="daily",
                                        flush_every=1, job_run_id=run_id)
        result["capture"] = captured
        if not captured.get("source_batch_id") or captured.get("raw_capture_failed"):
            raise RuntimeError("真实 Raw Batch 未成功落盘")

        runner.center.event(run_id, "采集完成，开始标准化构建", phase="数据构建", event_type="start")
        job_store.update_progress(run_id, phase="数据构建", progress=30)
        builder = DailyBuilder(warehouse)
        partition = end_date[:7]
        build = builder.build_partition(partition, include_current=False)
        result["build"] = {key: str(value) for key, value in build.items() if key != "diff_report"}
        state = PipelineState(warehouse.meta_db_path)
        version = state.create_version(build, source_batches=[captured["source_batch_id"]])
        quality = check_stock_daily(build["path"], expected_symbols=len(symbols), expected_trade_date=end_date,
                                    source_conflicts=build["source_conflicts"])
        state.quality(version, status=quality["status"], checks=quality["checks"],
                      publish_allowed=quality["publish_allowed"])
        result["quality"] = quality
        if not quality["publish_allowed"]:
            raise RuntimeError(f"Shadow 数据质量不允许发布: {quality['status']}")
        runner.center.event(run_id, "质量检查通过，开始隔离验证发布", phase="数据发布", event_type="start")
        job_store.update_progress(run_id, phase="数据发布", progress=50)
        result["publish"] = Publisher(warehouse).publish(version)

        runner.center.event(run_id, "开始技术指标计算", phase="派生计算", event_type="start")
        job_store.update_progress(run_id, phase="派生计算", progress=65)
        result["indicators"] = IndicatorsBuilder(warehouse, allow_legacy=False,
                                                   ).build_all(symbols=symbols, flush_every=1)
        runner.center.event(run_id, "开始研究因子计算", phase="派生计算", event_type="start")
        job_store.update_progress(run_id, phase="派生计算", progress=82)
        result["factors"] = FactorEngine(warehouse, allow_legacy=False).build_factors(symbols=symbols)
        result["artifacts"] = {
            "raw": captured.get("raw_batch"),
            "daily": str(warehouse.daily_partition(partition)),
            "candidate": str(build["path"]),
            "indicators": str(warehouse.indicator_dir / f"{partition}.parquet"),
            "factors": str(warehouse.factor_dir / f"{partition}.parquet"),
        }
        artifact_ids = {}
        for name, path in result["artifacts"].items():
            if isinstance(path, dict):
                path = path.get("path")
            if path and Path(path).exists():
                artifact_ids[name] = metadata.register_artifact(
                    run_id=run_id, dataset_name="stock_daily" if name in {"raw", "daily", "candidate"} else name,
                    artifact_type={"raw": "raw_batch", "daily": "published_dataset", "candidate": "candidate"}.get(name, f"{name}_output"),
                    partition_key=partition, file_path=path,
                )
        result["artifact_ids"] = artifact_ids
        for upstream, downstream, relation in (
            ("raw", "candidate", "raw_input"),
            ("candidate", "daily", "published_from_candidate"),
            ("daily", "indicators", "published_input"),
            ("daily", "factors", "published_input"),
        ):
            if upstream in artifact_ids and downstream in artifact_ids:
                metadata.link_lineage(artifact_ids[upstream], artifact_ids[downstream], relation)
        runner.center.update_request(request_id, "success")
        result["status"] = "success"
        job_store.update_progress(run_id, phase="完成", progress=100)
        job_store.finish(run_id, "success", result)
        return result
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        runner.center.update_request(request_id, "failed")
        job_store.finish(run_id, "failed", result, error=str(exc))
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实小批量 Shadow 数据流程")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    args = parser.parse_args(argv)
    symbols = [item.strip().lower().replace(".", "") for item in args.symbols.split(",") if item.strip()]
    end = args.end or _business_end(datetime.now()).strftime("%Y-%m-%d")
    start = args.start or (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
    result = run_shadow(args.root, symbols, start, end)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
