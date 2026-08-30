#!/usr/bin/env python3
"""Repair historical adoption metadata without deleting any data files."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd


def repair(management_db: Path, warehouse: Path) -> dict:
    warehouse = Path(warehouse).resolve()
    repaired = 0
    removed_current = 0
    with sqlite3.connect(management_db) as conn:
        rows = conn.execute(
            "SELECT partition_key,version_id FROM dataset_current "
            "WHERE dataset_name='fundamentals' AND partition_key LIKE 'legacy_%'"
        ).fetchall()
        for partition, version_id in rows:
            conn.execute("DELETE FROM dataset_current WHERE dataset_name='fundamentals' AND partition_key=?", (partition,))
            conn.execute("UPDATE dataset_versions SET publish_status='superseded' WHERE version_id=?", (version_id,))
            removed_current += 1
        source_files = sorted((warehouse / "fundamentals").glob("*.parquet"))
        source_files = [path for path in source_files if not path.name.startswith("legacy_")]
        for path in source_files:
            code = path.stem
            frame = pd.read_parquet(path, columns=["stat_date"])
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            version_id = f"fundamentals_{code}_legacy_{checksum[:12]}"
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute("""INSERT OR IGNORE INTO dataset_versions
                (version_id,dataset_name,partition_key,candidate_path,published_path,source_batches,row_count,
                 symbol_count,min_date,max_date,schema_version,checksum,builder_version,quality_status,publish_status,
                 created_at,published_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (version_id, "fundamentals", code, str(path), str(path), "[]", len(frame), 1,
                 str(pd.to_datetime(frame["stat_date"]).min())[:10], str(pd.to_datetime(frame["stat_date"]).max())[:10],
                 "fundamentals.v1", checksum, "legacy_adoption.v1", "PASS", "published", now, now))
            conn.execute("""INSERT OR IGNORE INTO dataset_quality_results
                (quality_id,dataset_name,partition_key,version_id,status,checks,affected_symbols,publish_allowed,checked_at,checker_version)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (f"quality_{version_id}", "fundamentals", code, version_id, "PASS",
                 json.dumps({"legacy_repair": True}), "[]", 1, now, "legacy_repair.v1"))
            conn.execute("INSERT OR REPLACE INTO dataset_current(dataset_name,partition_key,version_id,published_at) VALUES(?,?,?,?)",
                         ("fundamentals", code, version_id, now))
            repaired += 1
    return {"canonical_current": repaired, "legacy_current_removed": removed_current,
            "files_preserved": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description="修复历史 fundamentals 接管元数据")
    parser.add_argument("--management-db", type=Path, default=Path("output/data/management.db"))
    parser.add_argument("--warehouse", type=Path, default=Path("output/data/warehouse"))
    args = parser.parse_args(argv)
    print(json.dumps(repair(args.management_db, args.warehouse), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
