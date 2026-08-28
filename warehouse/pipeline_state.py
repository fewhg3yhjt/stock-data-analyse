"""Version, quality and publish state for staged datasets."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4


def _now():
    return datetime.now().isoformat(timespec="seconds")


class PipelineState:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS dataset_versions (
              version_id TEXT PRIMARY KEY, dataset_name TEXT NOT NULL, partition_key TEXT NOT NULL,
              candidate_path TEXT, published_path TEXT, previous_version_id TEXT, input_versions TEXT,
              source_batches TEXT NOT NULL, row_count INTEGER NOT NULL, symbol_count INTEGER NOT NULL,
              min_date TEXT, max_date TEXT, schema_version TEXT NOT NULL, checksum TEXT NOT NULL,
              generated_by_job INTEGER, builder_version TEXT NOT NULL, quality_status TEXT,
              publish_status TEXT NOT NULL, created_at TEXT NOT NULL, validated_at TEXT,
              published_at TEXT, error_summary TEXT
            );
            CREATE TABLE IF NOT EXISTS dataset_current (
              dataset_name TEXT NOT NULL, partition_key TEXT NOT NULL, version_id TEXT NOT NULL,
              published_at TEXT NOT NULL, PRIMARY KEY(dataset_name, partition_key)
            );
            CREATE TABLE IF NOT EXISTS dataset_quality_results (
              quality_id TEXT PRIMARY KEY, dataset_name TEXT NOT NULL, partition_key TEXT NOT NULL,
              version_id TEXT NOT NULL, status TEXT NOT NULL, checks TEXT NOT NULL,
              affected_symbols TEXT, publish_allowed INTEGER NOT NULL, checked_at TEXT NOT NULL,
              checker_version TEXT NOT NULL
            );
            """)

    def create_version(self, build: dict, *, source_batches: list[str], builder_version: str = "daily_builder.v1") -> str:
        version = build["version_id"]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""INSERT INTO dataset_versions
              (version_id,dataset_name,partition_key,candidate_path,source_batches,row_count,symbol_count,
               min_date,max_date,schema_version,checksum,builder_version,publish_status,created_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (version, "stock_daily", build["partition"],
              str(build["path"]), json.dumps(source_batches), build["row_count"], build["symbol_count"],
              None, None, "stock_daily.v1", build["checksum"], builder_version, "candidate", _now()))
        return version

    def quality(self, version_id: str, *, status: str, checks: dict,
                publish_allowed: bool, affected_symbols: list[str] | None = None) -> str:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT dataset_name,partition_key FROM dataset_versions WHERE version_id=?", (version_id,)).fetchone()
            if not row:
                raise ValueError("版本不存在")
            quality_id = f"quality_{uuid4().hex[:12]}"
            conn.execute("INSERT INTO dataset_quality_results VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (quality_id, row[0], row[1], version_id, status, json.dumps(checks),
                          json.dumps(affected_symbols or []),
                          int(publish_allowed), _now(), "quality.v1"))
            conn.execute("UPDATE dataset_versions SET quality_status=?,validated_at=? WHERE version_id=?",
                         (status, _now(), version_id))
        return quality_id

    def current(self, dataset_name: str, partition: str):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT version_id FROM dataset_current WHERE dataset_name=? AND partition_key=?",
                                (dataset_name, partition)).fetchone()
