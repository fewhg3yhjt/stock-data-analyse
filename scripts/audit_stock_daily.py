#!/usr/bin/env python3
"""Produce a read-only stock_daily baseline report."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from StockInvestmentTool.warehouse.baseline import audit_stock_daily, write_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审计 stock_daily 月分区（只读）")
    parser.add_argument("--warehouse", type=Path, required=True, help="warehouse 目录")
    parser.add_argument("--months", default="", help="限定月份，逗号分隔")
    parser.add_argument("--output", type=Path, default=None, help="JSON 报告路径")
    args = parser.parse_args(argv)
    months = [item.strip() for item in args.months.split(",") if item.strip()] or None
    report = audit_stock_daily(args.warehouse, months)
    output = args.output or Path("output/reports") / f"stock_daily_baseline_{datetime.now():%Y%m%d_%H%M%S}.json"
    write_report(report, output)
    print(f"报告已写入: {output}")
    print(f"分区: {report['partition_count']}, 行数: {report['row_count']}, 重复主键: {report['duplicate_primary_keys']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
