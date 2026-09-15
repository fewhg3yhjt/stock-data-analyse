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
            _write(log, {"event": "month_done", "month": month,
                          "elapsed_sec": round(time.monotonic() - month_started, 1)})


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
