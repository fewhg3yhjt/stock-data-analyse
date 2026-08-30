#!/usr/bin/env python3
"""Reconcile migration candidates using the read-only source audit metadata."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


def reconcile(source_audit: Path, candidates: Path) -> dict:
    source = json.loads(Path(source_audit).read_text(encoding="utf-8"))
    candidate = json.loads(Path(candidates).read_text(encoding="utf-8"))
    source_by_path = {}
    for dataset in source["datasets"].values():
        for record in dataset["records"]:
            source_by_path[record["path"]] = record
    result = {"report_type": "migration_candidate_reconciliation",
              "generated_at": datetime.now().isoformat(timespec="seconds"),
              "metadata_only": True, "datasets": {}}
    for dataset in ("stock_daily", "fundamentals", "valuation_daily"):
        rows = [record for record in candidate["records"] if record["dataset"] == dataset]
        mismatches = []
        source_rows = candidate_rows = 0
        source_symbols = candidate_symbols = set()
        for record in rows:
            original = source_by_path.get(record["source_path"])
            if original is None:
                mismatches.append({"source_path": record["source_path"], "reason": "missing_source_audit"})
                continue
            source_rows += original.get("row_count", 0) or 0
            candidate_rows += record.get("row_count", 0) or 0
            if original.get("symbol_count") is not None:
                source_symbols.add((record["source_path"], original["symbol_count"]))
            if record.get("symbol_count") is not None:
                candidate_symbols.add((record["source_path"], record["symbol_count"]))
            if original.get("row_count") != record.get("row_count"):
                mismatches.append({"source_path": record["source_path"], "reason": "row_count_changed",
                                   "source_rows": original.get("row_count"), "candidate_rows": record.get("row_count")})
        result["datasets"][dataset] = {
            "file_count": len(rows), "source_rows": source_rows, "candidate_rows": candidate_rows,
            "row_delta": candidate_rows - source_rows,
            "source_symbol_sum": sum(value for _, value in source_symbols),
            "candidate_symbol_sum": sum(value for _, value in candidate_symbols),
            "mismatch_count": len(mismatches), "mismatches": mismatches[:100],
            "status": "PASS" if not mismatches else "WARNING",
        }
    result["status"] = "PASS" if all(item["status"] == "PASS" for item in result["datasets"].values()) else "WARNING"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="元数据级历史迁移候选对账")
    parser.add_argument("--source-audit", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = reconcile(args.source_audit, args.candidates)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "datasets": report["datasets"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
