#!/usr/bin/env python3
"""Build isolated migration candidates from existing production files."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from StockInvestmentTool.warehouse.legacy_adapters import adapt_legacy_file


LAYOUTS = {
    "stock_daily": "daily",
    "fundamentals": "fundamentals",
    "valuation_daily": "raw/valuation",
}


def build_candidates(warehouse: Path, output: Path, datasets: list[str] | None = None) -> dict:
    warehouse = Path(warehouse).resolve()
    output = Path(output).resolve()
    selected = datasets or list(LAYOUTS)
    records = []
    for dataset in selected:
        directory = warehouse / LAYOUTS[dataset]
        files = sorted(directory.glob("*.parquet"))
        for source in files:
            if dataset == "fundamentals":
                target_name = source.stem
            else:
                target_name = source.stem
            record = {"dataset": dataset, "source_path": str(source),
                      "source_checksum": hashlib.sha256(source.read_bytes()).hexdigest()}
            try:
                frame = adapt_legacy_file(dataset, source)
                target = output / dataset / target_name / f"legacy_{target_name}_{record['source_checksum'][:12]}.parquet"
                target.parent.mkdir(parents=True, exist_ok=True)
                frame.to_parquet(target, index=False, engine="pyarrow", compression="zstd")
                record.update({"candidate_path": str(target), "row_count": len(frame),
                               "symbol_count": int(frame["code"].nunique()) if "code" in frame else 0,
                               "columns": list(frame.columns), "status": "candidate"})
            except Exception as exc:
                record.update({"status": "quarantine", "error": f"{type(exc).__name__}: {exc}"})
            records.append(record)
    report = {"report_type": "legacy_migration_candidates", "generated_at": datetime.now().isoformat(timespec="seconds"),
              "read_only_input": True, "warehouse": str(warehouse), "output": str(output), "records": records}
    output.mkdir(parents=True, exist_ok=True)
    (output / "migration_candidates.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成隔离历史迁移 Candidate")
    parser.add_argument("--warehouse", type=Path, default=Path("output/data/warehouse"))
    parser.add_argument("--output", type=Path, default=Path("output/data/migration_candidates"))
    parser.add_argument("--datasets", default="stock_daily,fundamentals,valuation_daily")
    args = parser.parse_args(argv)
    report = build_candidates(args.warehouse, args.output, [x.strip() for x in args.datasets.split(",") if x.strip()])
    print(json.dumps({"output": str(args.output), "records": len(report["records"]),
                      "candidate": sum(r["status"] == "candidate" for r in report["records"]),
                      "quarantine": sum(r["status"] == "quarantine" for r in report["records"])}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
