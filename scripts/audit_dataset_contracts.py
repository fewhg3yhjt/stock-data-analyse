#!/usr/bin/env python3
"""只读数据契约诊断（阶段一）：输出各数据集契约事实与严重低覆盖告警。

用法：
    python -m StockInvestmentTool.warehouse.contract_diagnostics [--dataset NAME] [--partition KEY] [--output PATH]
    # 或
    python scripts/audit_dataset_contracts.py [--dataset NAME] [--output PATH]

根目录默认使用 Config.DATA_DIR（output/data），与 YAML 中 `warehouse/...` 相对路径一致。
诊断不修改任何生产数据库或数据文件。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from StockInvestmentTool.config import Config
from StockInvestmentTool.warehouse.contract_diagnostics import (
    coverage_alert,
    diagnose_all,
    write_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读数据契约诊断")
    parser.add_argument("--dataset", default=None, help="仅诊断指定数据集")
    parser.add_argument("--partition", default=None, help="仅诊断指定分区")
    parser.add_argument("--warehouse-root", type=Path, default=Config.DATA_DIR,
                        help="数据根目录（默认 Config.DATA_DIR）")
    parser.add_argument("--db", type=Path, default=None,
                        help="管理库路径（默认 $MANAGEMENT_DB_PATH 或 output/data/management.db）")
    parser.add_argument("--output", type=Path, default=None, help="JSON 报告输出路径")
    args = parser.parse_args(argv)

    import os
    db_path = args.db or (Path(os.getenv("MANAGEMENT_DB_PATH")) if os.getenv("MANAGEMENT_DB_PATH") else args.warehouse_root / "management.db")
    report = diagnose_all(args.warehouse_root, db_path,
                          dataset_filter=args.dataset,
                          partition_filter=args.partition)
    alert_count = 0
    for name, rows in report["datasets"].items():
        alerts = coverage_alert(rows)
        alert_count += len(alerts)
        for a in alerts:
            print(f"[低覆盖] {a['dataset']} {a['partition']} "
                  f"{a['symbol_count']}/{a['expected_symbols']} = {a['coverage_ratio']}")
    if args.output:
        write_report(report, args.output)
        print(f"报告已写入: {args.output}")
    print(f"数据集: {len(report['datasets'])}，严重低覆盖告警: {alert_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())