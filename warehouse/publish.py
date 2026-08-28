"""Atomic candidate publication for staged dataset partitions."""

from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


class Publisher:
    def __init__(self, warehouse):
        self.warehouse = warehouse

    def publish(self, version_id: str) -> dict:
        with sqlite3.connect(self.warehouse.meta_db_path) as conn:
            row = conn.execute("SELECT dataset_name,partition_key,candidate_path,quality_status,publish_status FROM dataset_versions WHERE version_id=?", (version_id,)).fetchone()
            if not row or row[3] not in ("PASS", "WARNING"):
                raise ValueError("版本不存在或质量不允许发布")
            current = conn.execute("SELECT version_id FROM dataset_current WHERE dataset_name=? AND partition_key=?", (row[0], row[1])).fetchone()
            target = self.warehouse.daily_partition(row[1])
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.{version_id}.tmp")
            shutil.copyfile(row[2], temp)
            os.replace(temp, target)
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute("UPDATE dataset_versions SET published_path=?,previous_version_id=?,publish_status='published',published_at=? WHERE version_id=?",
                         (str(target), current[0] if current else None, now, version_id))
            conn.execute("INSERT INTO dataset_current (dataset_name,partition_key,version_id,published_at) VALUES (?,?,?,?) ON CONFLICT(dataset_name,partition_key) DO UPDATE SET version_id=excluded.version_id,published_at=excluded.published_at",
                         (row[0], row[1], version_id, now))
        return {"version_id": version_id, "path": str(target), "previous_version_id": current[0] if current else None}
