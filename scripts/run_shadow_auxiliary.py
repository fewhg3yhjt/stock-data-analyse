#!/usr/bin/env python3
"""Run auxiliary source tasks in an isolated shadow warehouse."""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from StockInvestmentTool.fundflow.capture import capture_money_flow
from StockInvestmentTool.ops.task_center import TaskCenter
from StockInvestmentTool.ops.task_runner import TaskRunner
from StockInvestmentTool.warehouse.backfill import ValuationBackfill
from StockInvestmentTool.warehouse.fundamentals_collect import FundamentalsCollector
from StockInvestmentTool.warehouse.industry import IndustryCollector, stage_and_publish_industry_batch
from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.storage import Warehouse


AUXILIARY_TASKS = {
    "industry_capture", "fundamentals_capture", "valuation_capture", "money_flow_capture",
}


def _seed_instruments(db_path: Path, symbols: list[str]) -> None:
    from StockInvestmentTool.warehouse.asset_profiles import asset_type_for
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS instruments (
            code TEXT PRIMARY KEY, name TEXT, type TEXT, board TEXT,
            listed_date TEXT, industry TEXT DEFAULT '', updated_at TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS fundamental_manifest (
            code TEXT PRIMARY KEY, rows INTEGER, last_period TEXT, updated_at TEXT
        )""")
        conn.executemany(
            "INSERT OR REPLACE INTO instruments(code,type,updated_at) VALUES(?,?,?)",
            [(code, asset_type_for(code), datetime.now().isoformat(timespec="seconds")) for code in symbols],
        )


def _quality(dataset: str, rows: int, symbols: int, expected: int, start: str, end: str) -> dict:
    coverage = symbols / expected if expected else 0.0
    return {
        "dataset": dataset, "rows": rows, "symbols": symbols, "expected_symbols": expected,
        "coverage": round(coverage, 4), "status": "PASS" if coverage >= 0.95 else "WARNING",
        "publish_allowed": coverage >= 0.95, "period_start": start, "period_end": end,
    }


def run_shadow_auxiliary(root: Path, symbols: list[str], start: str, end: str) -> dict:
    root = Path(root).resolve()
    formal = (Path.cwd() / "output" / "data" / "warehouse").resolve()
    if formal == root or formal in root.parents or root in formal.parents:
        raise ValueError("Shadow 输出目录不能与正式 warehouse 目录重叠")
    stock_symbols = [code for code in symbols if code.startswith(("sh6", "sz0", "sz3", "bj4", "bj8"))]
    if not stock_symbols:
        raise ValueError("辅助数据验证至少需要 1 只股票")

    warehouse = Warehouse(root / "warehouse")
    warehouse.meta_db_path = root / "management.db"
    center = TaskCenter(warehouse.meta_db_path, warehouse.meta_db_path)
    for dataset in ("industry", "fundamentals", "valuation_daily", "money_flow_daily"):
        warehouse.metadata.register_dataset(dataset)
    center.sync_definitions()
    center.sync_metrics()
    _seed_instruments(warehouse.meta_db_path, symbols)
    runner = TaskRunner(warehouse.meta_db_path, warehouse.meta_db_path)
    result = {"root": str(root), "symbols": symbols, "stock_symbols": stock_symbols,
              "start_date": start, "end_date": end, "enabled_tasks": sorted(AUXILIARY_TASKS),
              "tasks": {}}

    def execute(task_key, worker, input_dataset, output_dataset):
        request_id = center.create_request(task_key, "shadow", period_start=start, period_end=end,
                                           symbols=stock_symbols, requested_by="shadow_auxiliary_script")
        item = runner.execute(task_key, worker, request_id=request_id,
                              input_dataset=input_dataset, output_dataset=output_dataset)
        result["tasks"][task_key] = item
        return item["result"], item["run_id"]

    # Use a private fetcher cache so this run never writes production cache files.
    from StockInvestmentTool.datasource.fetcher import StockDataFetcher
    fetcher = StockDataFetcher(data_dir=root / "cache")
    try:
        industry, industry_run = execute(
            "industry_capture",
            lambda run_id, request: _industry(warehouse, fetcher, stock_symbols),
            "universe", "industry",
        )
        fundamentals, fundamentals_run = execute(
            "fundamentals_capture",
            lambda run_id, request: _fundamentals(warehouse, fetcher, stock_symbols),
            "universe", "fundamentals",
        )
        valuation, valuation_run = execute(
            "valuation_capture",
            lambda run_id, request: _valuation(warehouse, stock_symbols, start, end),
            "stock_daily", "valuation_daily",
        )
        money_flow, money_flow_run = execute(
            "money_flow_capture",
            lambda run_id, request: _money_flow(warehouse),
            "universe", "money_flow_daily",
        )
        _register_artifacts(center, warehouse, result, {
            "industry_capture": (industry_run, industry),
            "fundamentals_capture": (fundamentals_run, fundamentals),
            "valuation_capture": (valuation_run, valuation),
            "money_flow_capture": (money_flow_run, money_flow),
        })
        result["status"] = "success"
        return result
    except Exception as exc:
        result.update({"status": "failed", "error": str(exc)})
        raise


def _industry(warehouse, fetcher, symbols):
    result = IndustryCollector(warehouse).collect_membership(
        codes=symbols, snapshot_date=datetime.now().strftime("%Y-%m-%d"))
    if result.get("raw_batch_id"):
        result["published"] = stage_and_publish_industry_batch(
            warehouse, dataset_name="industry_membership", batch_id=result["raw_batch_id"],
            expected_symbols=result.get("expected_symbols"))
    return result


def _fundamentals(warehouse, fetcher, symbols):
    collector = FundamentalsCollector(warehouse=warehouse, fetcher=fetcher)
    existing_before = {code for code in symbols if warehouse.fundamental_path(code).exists()}
    output = collector.collect_fundamentals(codes=symbols, years=5)
    covered = len(existing_before) + output.get("done", 0)
    output["rows"] = output.get("done", 0) + len(existing_before)
    output["symbols"] = covered
    output["skipped"] = False
    output["quality"] = _quality("fundamentals", output["rows"], covered, len(symbols), "", "")
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
                         trade_date_start=start, trade_date_end=end,
                         expected_symbols=len(symbols), success_symbols=len(frames), failed_symbols=len(failed),
                         universe_id="shadow_auxiliary_valuation") if frames else None
    rows = sum(len(frame) for frame in frames)
    return {"rows": rows, "symbols": len(frames), "failed": failed,
            "raw_batch_id": raw["batch_id"] if raw else None,
            "quality": _quality("valuation_daily", rows, len(frames), len(symbols), start, end)}


def _money_flow(warehouse):
    output = capture_money_flow("stock", "now", warehouse=warehouse)
    output["quality"] = _quality("money_flow_daily", output["rows"], output["rows"], output["rows"], "", "")
    return output


def _register_artifacts(center, warehouse, result, tasks):
    for task_key, (run_id, payload) in tasks.items():
        batch_id = payload.get("raw_batch_id")
        if batch_id:
            with sqlite3.connect(warehouse.meta_db_path) as conn:
                row = conn.execute("SELECT raw_path FROM source_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if row and row[0] and Path(row[0]).exists():
                artifact_id = center.register_artifact(run_id=run_id, dataset_name=payload.get("quality", {}).get("dataset", task_key),
                                                       artifact_type="raw_batch", partition_key=None, file_path=row[0])
                result["tasks"][task_key]["artifact_id"] = artifact_id


def main(argv=None):
    parser = argparse.ArgumentParser(description="辅助数据 Shadow 验证")
    parser.add_argument("--root", type=Path, default=Path("output/data/shadow_validation_aux_100_1y"))
    parser.add_argument("--symbols-file", type=Path, default=Path("/tmp/shadow-stocks.txt"))
    parser.add_argument("--start", default=(datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"))
    parser.add_argument("--end", default=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"))
    args = parser.parse_args(argv)
    symbols = [line.strip().lower().replace(".", "") for line in args.symbols_file.read_text().splitlines() if line.strip()]
    with contextlib.redirect_stdout(sys.stderr):
        result = run_shadow_auxiliary(args.root, symbols, args.start, args.end)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
