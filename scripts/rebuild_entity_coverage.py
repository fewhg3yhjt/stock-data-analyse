#!/usr/bin/env python3
"""Explicitly rebuild entity coverage from immutable Raw/Published files.

This is a bounded, operator-invoked maintenance tool. It is never called by
the daily scheduler or Data Worker automatically.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from StockInvestmentTool.warehouse.coverage import CoverageStore
from StockInvestmentTool.warehouse.storage import Warehouse


def rebuild(warehouse: Warehouse, *, dataset_name: str, source_name: str,
            entity_type: str, files: list[Path]) -> int:
    """Rebuild latest success dates for explicitly supplied Parquet files."""
    import pandas as pd

    store = CoverageStore(warehouse.meta_db_path)
    count = 0
    for path in files:
        frame = pd.read_parquet(path, columns=["date", "code"])
        for code, group in frame.groupby("code"):
            dates = [str(value)[:10] for value in pd.to_datetime(group["date"]).dropna().unique()]
            store.record_success(dataset_name=dataset_name, source_name=source_name,
                                 entity_type=entity_type, entity_id=str(code),
                                 data_dates=dates, batch_id=f"rebuild:{path.name}")
            count += 1
    return count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="重建数据实体覆盖索引（显式维护工具）")
    parser.add_argument("--warehouse", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--dataset", default="stock_daily")
    parser.add_argument("--source", default="tencent")
    parser.add_argument("--entity-type", choices=("stock", "etf", "index", "industry"), required=True)
    parser.add_argument("files", nargs="+", type=Path, help="显式指定要扫描的 Parquet 文件")
    args = parser.parse_args(argv)
    total = rebuild(Warehouse(args.warehouse, meta_db_path=args.db),
                    dataset_name=args.dataset, source_name=args.source,
                    entity_type=args.entity_type, files=args.files)
    print({"entities_updated": total})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
