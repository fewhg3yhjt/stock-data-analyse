#!/usr/bin/env python3
"""Audit isolated historical migration candidates without touching production facts."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


KEYS = {"stock_daily": ["date", "code"], "fundamentals": ["code", "stat_date"],
        "valuation_daily": ["date", "code"], "indicators": ["date", "code"],
        "factors": ["date", "code"], "industry": ["code"],
        "money_flow_daily": ["period", "code"]}


def audit(root: Path) -> dict:
    result = {"report_type": "migration_candidate_quality",
              "generated_at": datetime.now().isoformat(timespec="seconds"),
              "root": str(root), "datasets": {}}
    for dataset, keys in KEYS.items():
        records = []
        for path in sorted((root / dataset).rglob("*.parquet")):
            item = {"path": str(path), "readable": False, "status": "FAIL"}
            try:
                frame = pd.read_parquet(path)
                item["readable"] = True
                item["rows"] = len(frame)
                item["columns"] = list(frame.columns)
                item["missing_keys"] = [key for key in keys if key not in frame]
                item["duplicate_primary_keys"] = int(frame.duplicated(keys).sum()) if not item["missing_keys"] else None
                item["null_rates"] = {key: round(float(frame[key].isna().mean()), 6) for key in frame.columns}
                date_columns = [key for key in ("date", "stat_date") if key in frame]
                if date_columns:
                    values = pd.to_datetime(frame[date_columns[0]], errors="coerce")
                    item["min_date"] = str(values.min())[:10] if values.notna().any() else None
                    item["max_date"] = str(values.max())[:10] if values.notna().any() else None
                    item["invalid_date_count"] = int(values.isna().sum())
                item["symbol_count"] = int(frame["code"].astype(str).nunique()) if "code" in frame else 0
                item["status"] = "PASS" if not item["missing_keys"] and item["duplicate_primary_keys"] == 0 and item.get("invalid_date_count", 0) == 0 else "FAIL"
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
            records.append(item)
        result["datasets"][dataset] = {
            "file_count": len(records), "readable_count": sum(x["readable"] for x in records),
            "pass_count": sum(x["status"] == "PASS" for x in records),
            "fail_count": sum(x["status"] == "FAIL" for x in records), "records": records,
        }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="审计隔离迁移候选")
    parser.add_argument("--root", type=Path, default=Path("output/data/migration_candidates"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = audit(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: {x: v[x] for x in ("file_count", "readable_count", "pass_count", "fail_count")} for k, v in report["datasets"].items()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
