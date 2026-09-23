#!/usr/bin/env python3
"""Run controlled pending-code repair for an explicit reverse month range."""

from __future__ import annotations

import argparse
import json
import time
from datetime import date
from pathlib import Path

import pandas as pd

from StockInvestmentTool.warehouse.collector import MarketCollector
from StockInvestmentTool.warehouse.daily_build import DailyBuilder
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.quality import check_stock_daily
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.storage import Warehouse


def _months(start: str, end: str) -> list[str]:
    start_year, start_month = (int(item) for item in start.split("-"))
    end_year, end_month = (int(item) for item in end.split("-"))
    values = []
    year, month = start_year, start_month
    while (year, month) >= (end_year, end_month):
        values.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year -= 1
            month = 12
    return values


def run(root: Path, start: str, end: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        for month in _months(start, end):
            year, month_number = month.split("-")
            month_root = root / year / month_number
            pending_paths = sorted(month_root.glob("*/pending_codes.csv"))
            month_started = time.monotonic()
            _write(log, {"event": "month_start", "month": month,
                          "pending_files": len(pending_paths)})
            for pending_path in pending_paths:
                day = pending_path.parent.name
                pending_count = len(pd.read_csv(pending_path))
                if pending_count == 0:
                    continue
                _write(log, {"event": "day_start", "month": month,
                              "date": f"{month}-{day}", "pending": pending_count})
                started = time.monotonic()
                result = MarketCollector(query_interval=0.3).sync_daily(
                    start_date=f"{month}-{day}", end_date=f"{month}-{day}",
                    symbols=None, pending_codes_path=pending_path,
                    run_date=f"{month}-{day}", raw_subdir="_tmp",
                    raw_batch_size=50, auto_merge_effective=False,
                    force_refresh=True, source="tencent", target="raw:tencent",
                    timeout=3600, flush_every=50,
                )
                remaining = len(pd.read_csv(pending_path))
                _write(log, {"event": "day_done", "month": month,
                              "date": f"{month}-{day}",
                              "requested": result.get("symbols"),
                              "rows": result.get("added_rows"),
                              "failed": len(result.get("failed") or []),
                              "skipped": result.get("skipped_symbols"),
                              "timed_out": result.get("timed_out"),
                              "remaining": remaining,
                              "elapsed_sec": result.get("elapsed_sec"),
                              "started_epoch": started})
            pipeline = _publish_month(root, month)
            _write(log, {"event": "month_pipeline", "month": month, **pipeline})
            _write(log, {"event": "month_done", "month": month,
                          "elapsed_sec": round(time.monotonic() - month_started, 1)})


def _publish_month(root: Path, month: str) -> dict:
    """Build, quality-check and publish one completed month from its Raw files."""
    year, month_number = month.split("-")
    month_root = root / year / month_number
    files = sorted(month_root.glob("*/batch_repair_*.parquet"))
    if not files:
        return {"status": "blocked", "reason": "no_raw_files", "pending": 0}

    pending = sum(len(pd.read_csv(path)) for path in month_root.glob("*/pending_codes.csv"))
    raw = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
    warehouse = Warehouse()
    normalized = DailyBuilder(warehouse)._normalize(raw, "tencent", units={})
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    normalized["code"] = normalized["code"].astype(str).str.lower().str.replace(".", "", regex=False)
    normalized = normalized.sort_values(["date", "code"]).drop_duplicates(["date", "code"]).reset_index(drop=True)

    import hashlib
    fingerprint = hashlib.sha256(
        normalized.to_json(orient="records", date_format="iso").encode()
    ).hexdigest()
    candidate_dir = warehouse.base_dir / "candidates" / "stock_daily" / month
    candidate_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = candidate_dir / f"stock_daily_{month.replace('-', '')}_repaired_norm_{fingerprint[:12]}.parquet"
    normalized.to_parquet(candidate_path, index=False, engine="pyarrow", compression="zstd")
    version_id = f"stock_daily_{month.replace('-', '')}_repaired_norm_{fingerprint[:12]}"
    build = {
        "version_id": version_id, "partition": month, "path": candidate_path,
        "row_count": len(normalized), "symbol_count": int(normalized["code"].nunique()),
        "min_date": str(normalized["date"].min())[:10],
        "max_date": str(normalized["date"].max())[:10],
        "checksum": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
    }
    state = PipelineState(warehouse.meta_db_path)
    state.create_version(build, source_batches=[f"raw-repair:{month}:{len(files)}"])
    quality = check_stock_daily(candidate_path, expected_symbols=6860,
                                expected_trade_date=None, source_conflicts=[])
    state.quality(version_id, status=quality["status"], checks=quality["checks"],
                  publish_allowed=quality["publish_allowed"])
    result = {
        "status": quality["status"], "publish_allowed": quality["publish_allowed"],
        "version_id": version_id, "rows": len(normalized),
        "symbols": int(normalized["code"].nunique()), "pending": pending,
        "coverage": quality["checks"].get("coverage"),
        "unit_anomalies": quality["checks"]["unit_consistency"]["abnormal_count"],
    }
    if quality["publish_allowed"]:
        result["publish"] = Publisher(warehouse).publish(version_id)
    else:
        result["publish"] = None
    return result


def _write(handle, payload: dict) -> None:
    handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    handle.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--start-month", required=True)
    parser.add_argument("--end-month", required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    run(args.root, args.start_month, args.end_month, args.log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
