"""Small repeatable benchmark for warehouse chart reads."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from StockInvestmentTool.datasource.base import WarehouseSource
from StockInvestmentTool.warehouse.storage import Warehouse


def benchmark(code: str, days: int = 750, repeat: int = 3) -> dict:
    source = WarehouseSource()
    elapsed = []
    rows = 0
    for _ in range(max(1, repeat)):
        started = time.perf_counter()
        frame = source.fetch_daily_series(code, days)
        elapsed.append(time.perf_counter() - started)
        rows = len(frame)
    return {
        "code": code, "days": days, "rows": rows,
        "repeat": len(elapsed), "min_ms": round(min(elapsed) * 1000, 2),
        "avg_ms": round(sum(elapsed) / len(elapsed) * 1000, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark DuckDB chart reads")
    parser.add_argument("code", help="例如 sh600900")
    parser.add_argument("--days", type=int, default=750)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    print(benchmark(args.code, args.days, args.repeat))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
