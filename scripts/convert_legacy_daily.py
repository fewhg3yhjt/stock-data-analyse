#!/usr/bin/env python3
"""Convert legacy daily partitions into isolated migration candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd


def convert(input_dir: Path, output_dir: Path, months: list[str] | None = None) -> dict:
    selected = set(months or [])
    records = []
    for source in sorted(Path(input_dir).glob("*.parquet")):
        if selected and source.stem not in selected:
            continue
        frame = pd.read_parquet(source)
        source_checksum = hashlib.sha256(source.read_bytes()).hexdigest()
        target = output_dir / "stock_daily" / source.stem / f"migrated_{source.stem}_{source_checksum[:12]}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(target, index=False, engine="pyarrow", compression="zstd")
        records.append({"source_path": str(source), "source_checksum": source_checksum,
                        "output_path": str(target), "row_count": len(frame),
                        "columns": list(frame.columns), "source_name": "legacy_daily"})
    report = {"input_dir": str(input_dir), "output_dir": str(output_dir), "records": records,
              "read_only_input": True}
    report_path = output_dir / "legacy_daily_conversion.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="迁移旧 daily 分区到隔离候选目录")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--months", default="")
    args = parser.parse_args(argv)
    report = convert(args.input, args.output, [x.strip() for x in args.months.split(",") if x.strip()] or None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
