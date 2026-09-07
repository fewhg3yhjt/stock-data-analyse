"""Controlled Tencent Raw recapture for stock_daily unit repair.

The script only captures one immutable Raw Batch.  It never builds or
publishes a stock_daily partition; build/quality/publish remain explicit
separate operations.
"""

from __future__ import annotations

import argparse
import json

from StockInvestmentTool.warehouse.collector import MarketCollector


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture Tencent Raw units for stock_daily repair")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--job-run-id", type=int)
    args = parser.parse_args()
    result = MarketCollector(query_interval=0.3).capture_tencent_raw_units(
        start_date=args.start_date, end_date=args.end_date, job_run_id=args.job_run_id,
    )
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
