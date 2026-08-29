#!/usr/bin/env python3
"""Create and populate the unified management database without changing legacy DBs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from StockInvestmentTool.ops.management_db import ManagementDB


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="迁移任务和数据管理记录到统一管理库")
    parser.add_argument("--output", type=Path, required=True, help="新 management.db 路径")
    parser.add_argument("--job-db", type=Path, default=None, help="旧 job_runs.db")
    parser.add_argument("--warehouse-db", type=Path, default=None, help="旧 warehouse/meta.db")
    args = parser.parse_args(argv)
    db = ManagementDB(args.output)
    result = db.migrate_from(job_db=args.job_db, warehouse_db=args.warehouse_db)
    result["output"] = str(args.output)
    result["counts"] = db.counts()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
