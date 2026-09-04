#!/usr/bin/env python3
"""Read-only audit of existing warehouse files before historical adoption."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


DATASET_LAYOUTS = {
    "stock_daily": ("daily", ["date", "code"]),
    "indicators": ("indicators", ["date", "code"]),
    "fundamentals": ("fundamentals", ["code", "stat_date"]),
    "valuation_daily": ("raw/valuation", ["date", "code"]),
    "money_flow_daily": ("raw/ths/money_flow_daily", ["period", "code"]),
    "industry": ("raw/baostock/industry", ["code"]),
}


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_fields(dataset: str) -> list[str]:
    config = Path(__file__).resolve().parents[1] / "config" / "datasets" / f"{dataset}.yaml"
    if not config.exists():
        return []
    data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    return [field["name"] for field in data.get("fields", [])]


def audit_file(dataset: str, path: Path, primary_keys: list[str]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "dataset": dataset,
        "path": str(path),
        "file_name": path.name,
        "file_size": path.stat().st_size,
        "checksum": None,
        "readable": False,
        "migration_class": "quarantine",
        "schema_status": "unreadable",
        "quality_status": "FAIL",
    }
    try:
        item["checksum"] = checksum(path)
        frame = pd.read_parquet(path)
    except Exception as exc:
        item["error"] = f"{type(exc).__name__}: {exc}"
        return item

    item["readable"] = True
    item["columns"] = list(frame.columns)
    item["schema"] = [{"name": name, "dtype": str(dtype)} for name, dtype in frame.dtypes.items()]
    required = expected_fields(dataset) or primary_keys
    missing = [field for field in required if field not in frame.columns]
    item["missing_fields"] = missing
    item["extra_fields"] = [field for field in frame.columns if field not in required]
    item["schema_status"] = "PASS" if not missing else "FAIL"
    item["row_count"] = int(len(frame))
    item["symbol_count"] = int(frame["code"].astype(str).nunique()) if "code" in frame else None
    for date_column in ("date", "stat_date"):
        if date_column in frame:
            dates = pd.to_datetime(frame[date_column], errors="coerce")
            item["min_date"] = str(dates.min())[:10] if dates.notna().any() else None
            item["max_date"] = str(dates.max())[:10] if dates.notna().any() else None
            item["invalid_date_count"] = int(dates.isna().sum())
            break
    item["null_rates"] = {
        column: round(float(frame[column].isna().mean()), 6) for column in frame.columns
    }
    if all(key in frame.columns for key in primary_keys):
        item["duplicate_primary_keys"] = int(frame.duplicated(primary_keys).sum())
    else:
        item["duplicate_primary_keys"] = None
    item["migration_class"] = "metadata_adoption" if item["schema_status"] == "PASS" else "adapter_rebuild"
    item["quality_status"] = "PASS" if item["schema_status"] == "PASS" and item["duplicate_primary_keys"] == 0 else "FAIL"
    return item


def audit_warehouse(warehouse_dir: Path) -> dict[str, Any]:
    warehouse_dir = Path(warehouse_dir).resolve()
    datasets: dict[str, Any] = {}
    all_files: set[Path] = set()
    for dataset, (relative, primary_keys) in DATASET_LAYOUTS.items():
        directory = warehouse_dir / relative
        files = sorted(directory.rglob("*.parquet")) if directory.exists() else []
        all_files.update(files)
        records = [audit_file(dataset, path, primary_keys) for path in files]
        datasets[dataset] = {
            "path": str(directory),
            "primary_keys": primary_keys,
            "expected_fields": expected_fields(dataset),
            "file_count": len(records),
            "readable_count": sum(1 for record in records if record["readable"]),
            "quarantine_count": sum(1 for record in records if record["migration_class"] == "quarantine"),
            "adapter_rebuild_count": sum(1 for record in records if record["migration_class"] == "adapter_rebuild"),
            "metadata_adoption_count": sum(1 for record in records if record["migration_class"] == "metadata_adoption"),
            "quality_pass_count": sum(1 for record in records if record["quality_status"] == "PASS"),
            "duplicate_primary_keys": sum(record.get("duplicate_primary_keys") or 0 for record in records),
            "records": records,
        }
    temp_files = sorted(warehouse_dir.rglob("*.tmp")) + sorted(warehouse_dir.rglob("*.tmp.*"))
    report = {
        "report_type": "historical_data_migration_audit",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "warehouse_dir": str(warehouse_dir),
        "read_only": True,
        "datasets": datasets,
        "temporary_files": [str(path) for path in temp_files],
        "unclassified_parquet_files": [str(path) for path in sorted(set(warehouse_dir.rglob("*.parquet")) - all_files)],
        "summary": {
            "dataset_count": len(datasets),
            "file_count": sum(item["file_count"] for item in datasets.values()),
            "readable_count": sum(item["readable_count"] for item in datasets.values()),
            "quality_pass_count": sum(item["quality_pass_count"] for item in datasets.values()),
            "quarantine_count": sum(item["quarantine_count"] for item in datasets.values()),
            "adapter_rebuild_count": sum(item["adapter_rebuild_count"] for item in datasets.values()),
            "metadata_adoption_count": sum(item["metadata_adoption_count"] for item in datasets.values()),
            "temporary_file_count": len(temp_files),
        },
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读审计历史仓库迁移条件")
    parser.add_argument("--warehouse", type=Path, default=Path("output/data/warehouse"))
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    report = audit_warehouse(args.warehouse)
    output = args.output or Path("output/reports") / f"historical_data_migration_audit_{datetime.now():%Y%m%d%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
