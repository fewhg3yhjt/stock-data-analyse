#!/usr/bin/env python3
"""Run a bounded real-source pipeline in an isolated shadow warehouse."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from StockInvestmentTool.ops.job_runs import JobRunStore
from StockInvestmentTool.ops.task_center import TaskCenter
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
    run_id = job_store.start("shadow_pipeline", run_date=end_date, display_name="生产小批量 Shadow 数据流程",
                             input_dataset="source", output_dataset="factors")
    result = {"root": str(root), "symbols": symbols, "start_date": start_date, "end_date": end_date,
              "run_id": run_id, "trigger_type": "manual_shadow", "timezone": "Asia/Shanghai"}
    try:
        job_store.update_progress(run_id, phase="真实腾讯采集", progress=5, total=len(symbols))
        collector = __import__("StockInvestmentTool.warehouse.collector", fromlist=["MarketCollector"]).MarketCollector(
            warehouse=warehouse, query_interval=0.3
        )
        captured = collector.sync_daily(start_date=start_date, end_date=end_date, symbols=symbols,
                                        include_etf=True, source="tencent", target="daily",
                                        flush_every=1, job_run_id=run_id)
        result["capture"] = captured
        if not captured.get("source_batch_id") or captured.get("raw_capture_failed"):
            raise RuntimeError("真实 Raw Batch 未成功落盘")

        job_store.update_progress(run_id, phase="标准化构建", progress=30)
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
        job_store.update_progress(run_id, phase="Shadow 发布", progress=50)
        result["publish"] = Publisher(warehouse).publish(version)

        job_store.update_progress(run_id, phase="指标计算", progress=65)
        result["indicators"] = IndicatorsBuilder(warehouse, allow_legacy=False,
                                                   ).build_all(symbols=symbols, flush_every=1)
        job_store.update_progress(run_id, phase="因子计算", progress=82)
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
        result["status"] = "success"
        job_store.update_progress(run_id, phase="完成", progress=100)
        job_store.finish(run_id, "success", result)
        return result
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
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
