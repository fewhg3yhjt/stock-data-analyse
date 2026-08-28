"""Version, quality and publish state for staged datasets."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd


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
            columns = {row[1] for row in conn.execute("PRAGMA table_info(dataset_versions)")}
            if "rollback_path" not in columns:
                conn.execute("ALTER TABLE dataset_versions ADD COLUMN rollback_path TEXT")

    def create_version(self, build: dict, *, source_batches: list[str],
                       builder_version: str = "daily_builder.v1",
                       dataset_name: str = "stock_daily",
                       schema_version: str = "stock_daily.v1",
                       input_versions: dict | None = None,
                       publish_status: str = "candidate") -> str:
        version = build["version_id"]
        with sqlite3.connect(self.db_path) as conn:
            existing = conn.execute("SELECT version_id FROM dataset_versions WHERE version_id=?", (version,)).fetchone()
            if existing:
                return version
            conn.execute("""INSERT INTO dataset_versions
              (version_id,dataset_name,partition_key,candidate_path,source_batches,row_count,symbol_count,
               min_date,max_date,schema_version,checksum,builder_version,publish_status,created_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (version, dataset_name, build["partition"],
              str(build["path"]), json.dumps(source_batches), build["row_count"], build["symbol_count"],
              build.get("min_date"), build.get("max_date"), schema_version, build["checksum"],
              builder_version, publish_status, _now()))
            if input_versions:
                conn.execute("UPDATE dataset_versions SET input_versions=? WHERE version_id=?",
                             (json.dumps(input_versions, ensure_ascii=False), version))
        return version

    def record_output_versions(self, *, dataset_name: str, paths: dict[str, Path],
                               input_dataset: str, input_versions: dict,
                               builder_version: str, schema_version: str) -> dict[str, str]:
        """Record already-written derived partitions as published outputs."""
        import hashlib
        versions = {}
        with sqlite3.connect(self.db_path) as conn:
            for partition, path in paths.items():
                if not path.exists():
                    continue
                output_checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                input_fingerprint = hashlib.sha256(
                    json.dumps(input_versions, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()
                version_id = f"{dataset_name}_{partition.replace('-', '')}_{output_checksum[:10]}_{input_fingerprint[:10]}"
                checksum = output_checksum
                previous = conn.execute(
                    "SELECT version_id FROM dataset_current WHERE dataset_name=? AND partition_key=?",
                    (dataset_name, partition),
                ).fetchone()
                frame = pd.read_parquet(path, columns=["date", "code"])
                min_date = str(pd.to_datetime(frame["date"]).min())[:10] if not frame.empty else None
                max_date = str(pd.to_datetime(frame["date"]).max())[:10] if not frame.empty else None
                conn.execute("""INSERT OR IGNORE INTO dataset_versions
                    (version_id,dataset_name,partition_key,candidate_path,published_path,previous_version_id,
                     input_versions,source_batches,row_count,symbol_count,min_date,max_date,schema_version,
                     checksum,builder_version,publish_status,created_at,published_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (version_id, dataset_name, partition, str(path), str(path), previous[0] if previous else None,
                     json.dumps({input_dataset: input_versions}, ensure_ascii=False), "[]", len(frame),
                     int(frame["code"].nunique()) if "code" in frame else 0, min_date, max_date,
                     schema_version, checksum, builder_version, "published", _now(), _now()))
                conn.execute("""INSERT INTO dataset_current (dataset_name,partition_key,version_id,published_at)
                    VALUES (?,?,?,?) ON CONFLICT(dataset_name,partition_key) DO UPDATE SET
                    version_id=excluded.version_id,published_at=excluded.published_at""",
                    (dataset_name, partition, version_id, _now()))
                conn.execute("""INSERT OR IGNORE INTO dataset_quality_results
                    (quality_id,dataset_name,partition_key,version_id,status,checks,affected_symbols,
                     publish_allowed,checked_at,checker_version)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (f"quality_{version_id}", dataset_name, partition, version_id, "PASS",
                     json.dumps({"derived_output": True}, ensure_ascii=False), "[]", 1, _now(),
                    "derived_output.v1"))
                conn.execute("UPDATE dataset_versions SET quality_status='PASS',validated_at=? WHERE version_id=?",
                             (_now(), version_id))
                versions[partition] = version_id
        return versions

    def update_rollback_path(self, version_id: str, path: Path) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE dataset_versions SET rollback_path=? WHERE version_id=?", (str(path), version_id))

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
