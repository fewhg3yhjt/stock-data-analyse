#!/usr/bin/env python3
"""Generate deterministic, persistent fixtures for later pipeline validation."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from StockInvestmentTool.warehouse.metadata import MetadataStore
from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.storage import Warehouse


def generate(root: Path) -> dict:
    root = Path(root)
    warehouse = Warehouse(root / "warehouse")
    metadata = MetadataStore(warehouse.meta_db_path)
    configs = {}
    for name in ("stock_daily", "industry", "fundamentals", "valuation_daily", "money_flow_daily"):
        metadata.register_dataset(name)
        configs[name] = True

    dates = pd.to_datetime(["2026-08-27", "2026-08-28"])
    daily = pd.DataFrame({
        "date": dates.tolist() * 2,
        "code": ["sh600000"] * 2 + ["sh600001"] * 2,
        "open": [10.0, 10.2, 20.0, 20.1], "high": [10.5, 10.6, 20.4, 20.5],
        "low": [9.8, 10.0, 19.7, 19.9], "close": [10.2, 10.4, 20.2, 20.3],
        "volume": [100.0, 120.0, 200.0, 220.0], "amount": [1000.0, 1200.0, 4000.0, 4400.0],
        "turn": [1.0, 1.2, 2.0, 2.2],
    })
    warehouse.write_daily_partition("2026-08", daily)
    metadata.index_daily_partitions(warehouse.daily_dir)

    industry = pd.DataFrame({"code": ["sh600000", "sh600001"], "industry": ["银行", "证券"]})
    fundamentals = pd.DataFrame({
        "code": ["sh600000", "sh600001"],
        "stat_date": pd.to_datetime(["2026-06-30", "2026-06-30"]),
        "roe": [8.1, 10.2], "gross_margin": [32.0, 28.0], "debt_ratio": [91.0, 84.0],
    })
    valuation = pd.DataFrame({
        "date": dates.tolist() * 2, "code": ["sh600000"] * 2 + ["sh600001"] * 2,
        "peTTM": [6.1, 6.2, 12.1, 12.2], "pbMRQ": [0.8, 0.82, 1.3, 1.31],
    })
    money_flow = pd.DataFrame({
        "period": ["now", "now"], "code": ["sh600000", "sh600001"],
        "name": ["样例银行", "样例证券"], "net": [1.2, -0.3], "amount": [8.0, 6.0],
    })
    captures = {
        "industry": capture_frames(warehouse, dataset_name="industry", source_name="baostock",
                                     frames=[industry], expected_symbols=2, success_symbols=2,
                                     universe_id="validation_industry", request_context={"fixture": True}),
        "fundamentals": capture_frames(warehouse, dataset_name="fundamentals", source_name="akshare",
                                         frames=[fundamentals], expected_symbols=2, success_symbols=2,
                                         universe_id="validation_fundamentals", request_context={"fixture": True}),
        "valuation_daily": capture_frames(warehouse, dataset_name="valuation_daily", source_name="eastmoney",
                                            frames=[valuation], expected_symbols=2, success_symbols=2,
                                            universe_id="validation_valuation", request_context={"fixture": True}),
        "money_flow_daily": capture_frames(warehouse, dataset_name="money_flow_daily", source_name="ths",
                                             frames=[money_flow], expected_symbols=2, success_symbols=2,
                                             universe_id="validation_money_flow", request_context={"fixture": True}),
    }
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "root": str(root), "datasets": configs,
        "daily_path": str(warehouse.daily_partition("2026-08")),
        "batches": {name: {"batch_id": item["batch_id"], "raw_path": str(item["raw"]["path"]),
                           "status": item["status"]} for name, item in captures.items()},
    }
    report_path = root / "reports" / "pipeline_validation.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    root = Path("output/validation/data_pipeline")
    report = generate(root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
