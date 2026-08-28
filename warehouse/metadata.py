"""Runtime metadata projection for declarative dataset definitions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from StockInvestmentTool.warehouse.baseline import _checksum, _schema
from StockInvestmentTool.warehouse.dataset_config import load_dataset_config


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class MetadataStore:
    """Project YAML dataset definitions into queryable SQLite metadata."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS dataset_registry (
                    dataset_name TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    description TEXT NOT NULL, grain TEXT NOT NULL,
                    primary_keys TEXT NOT NULL, partition_type TEXT NOT NULL,
                    storage_path TEXT NOT NULL, update_frequency TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1, schema_version TEXT NOT NULL,
                    config_checksum TEXT NOT NULL DEFAULT '', config_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dataset_fields (
                    dataset_name TEXT NOT NULL, field_name TEXT NOT NULL,
                    display_name TEXT NOT NULL, data_type TEXT NOT NULL, unit TEXT,
                    nullable INTEGER NOT NULL DEFAULT 1, is_primary_key INTEGER NOT NULL DEFAULT 0,
                    description TEXT, schema_version TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(dataset_name, field_name)
                );
                CREATE TABLE IF NOT EXISTS dataset_sources (
                    dataset_name TEXT NOT NULL, source_name TEXT NOT NULL,
                    role TEXT NOT NULL, priority INTEGER NOT NULL,
                    field_mapping TEXT NOT NULL, unit_conversions TEXT NOT NULL,
                    request_defaults TEXT, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(dataset_name, source_name)
                );
                CREATE TABLE IF NOT EXISTS dataset_consumers (
                    dataset_name TEXT NOT NULL, consumer_name TEXT NOT NULL,
                    consumer_type TEXT NOT NULL, fields_used TEXT,
                    purpose TEXT NOT NULL, required_quality TEXT NOT NULL,
                    fallback_policy TEXT NOT NULL, blocked_actions TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(dataset_name, consumer_name)
                );
                CREATE TABLE IF NOT EXISTS dataset_partitions (
                    dataset_name TEXT NOT NULL, partition_key TEXT NOT NULL,
                    file_path TEXT NOT NULL, min_date TEXT, max_date TEXT,
                    row_count INTEGER NOT NULL, symbol_count INTEGER NOT NULL,
                    file_size INTEGER, schema_hash TEXT, checksum TEXT,
                    current_version_id TEXT, status TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(dataset_name, partition_key)
                );
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(dataset_registry)")}
            if "config_checksum" not in columns:
                conn.execute("ALTER TABLE dataset_registry ADD COLUMN config_checksum TEXT NOT NULL DEFAULT ''")
            if "config_path" not in columns:
                conn.execute("ALTER TABLE dataset_registry ADD COLUMN config_path TEXT NOT NULL DEFAULT ''")
        from StockInvestmentTool.warehouse.source_batches import SourceBatchStore
        SourceBatchStore(self.db_path)

    def register_stock_daily(self, config_path: Path | str | None = None) -> None:
        config = load_dataset_config("stock_daily", config_path)
        dataset = config["dataset"]
        schema_version = dataset["schema_version"]
        now = _now()
        with self._connect() as conn:
            conn.execute("""INSERT INTO dataset_registry
                (dataset_name,display_name,description,grain,primary_keys,partition_type,
                 storage_path,update_frequency,enabled,schema_version,config_checksum,config_path,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(dataset_name) DO UPDATE SET
                display_name=excluded.display_name,description=excluded.description,grain=excluded.grain,
                primary_keys=excluded.primary_keys,partition_type=excluded.partition_type,
                storage_path=excluded.storage_path,update_frequency=excluded.update_frequency,
                schema_version=excluded.schema_version,config_checksum=excluded.config_checksum,
                config_path=excluded.config_path,updated_at=excluded.updated_at""",
                (dataset["name"], dataset["display_name"], dataset["description"], dataset["grain"],
                 json.dumps(dataset["primary_keys"], ensure_ascii=False), dataset["partition"]["type"],
                 dataset["partition"]["path"], dataset["update_frequency"], 1, schema_version,
                 config["_config_checksum"], config["_config_path"], now, now))
            field_names = [field["name"] for field in config["fields"]]
            source_names = [source["name"] for source in config["sources"]]
            consumer_names = [consumer["name"] for consumer in config["consumers"]]
            conn.execute(
                "DELETE FROM dataset_fields WHERE dataset_name=? AND field_name NOT IN (%s)"
                % ",".join("?" for _ in field_names), [dataset["name"], *field_names]
            )
            conn.execute(
                "DELETE FROM dataset_sources WHERE dataset_name=? AND source_name NOT IN (%s)"
                % ",".join("?" for _ in source_names), [dataset["name"], *source_names]
            )
            conn.execute(
                "DELETE FROM dataset_consumers WHERE dataset_name=? AND consumer_name NOT IN (%s)"
                % ",".join("?" for _ in consumer_names), [dataset["name"], *consumer_names]
            )
            for field in config["fields"]:
                conn.execute("""INSERT INTO dataset_fields
                    (dataset_name,field_name,display_name,data_type,unit,nullable,is_primary_key,
                     description,schema_version,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(dataset_name,field_name) DO UPDATE SET
                    display_name=excluded.display_name,data_type=excluded.data_type,unit=excluded.unit,
                    nullable=excluded.nullable,is_primary_key=excluded.is_primary_key,description=excluded.description,
                    schema_version=excluded.schema_version,updated_at=excluded.updated_at""",
                    (dataset["name"], field["name"], field["display_name"], field["data_type"],
                     field.get("unit"), int(field["nullable"]), int(field.get("primary_key", False)),
                     field.get("description", ""), schema_version, now, now))
            for source in config["sources"]:
                conn.execute("""INSERT INTO dataset_sources
                    (dataset_name,source_name,role,priority,field_mapping,unit_conversions,
                     request_defaults,enabled,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(dataset_name,source_name) DO UPDATE SET
                    role=excluded.role,priority=excluded.priority,field_mapping=excluded.field_mapping,
                    unit_conversions=excluded.unit_conversions,request_defaults=excluded.request_defaults,
                    updated_at=excluded.updated_at""",
                    (dataset["name"], source["name"], source["role"], source["priority"],
                     json.dumps(source.get("field_mapping", {}), ensure_ascii=False),
                     json.dumps(source.get("unit_conversions", {}), ensure_ascii=False),
                     json.dumps(source.get("request_defaults", {}), ensure_ascii=False), 1, now, now))
            for consumer in config["consumers"]:
                conn.execute("""INSERT INTO dataset_consumers
                    (dataset_name,consumer_name,consumer_type,fields_used,purpose,required_quality,
                     fallback_policy,blocked_actions,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(dataset_name,consumer_name) DO UPDATE SET
                    consumer_type=excluded.consumer_type,purpose=excluded.purpose,
                    required_quality=excluded.required_quality,fallback_policy=excluded.fallback_policy,
                    blocked_actions=excluded.blocked_actions,updated_at=excluded.updated_at""",
                    (dataset["name"], consumer["name"], consumer["type"],
                     json.dumps(consumer.get("fields_used"), ensure_ascii=False), consumer["purpose"],
                     consumer["required_quality"], consumer["fallback_policy"],
                     consumer.get("blocked_actions", ""), now, now))

    def index_daily_partitions(self, daily_dir: Path) -> int:
        count = 0
        with self._connect() as conn:
            for path in sorted(Path(daily_dir).glob("*.parquet")):
                df = pd.read_parquet(path)
                dates = pd.to_datetime(df["date"], errors="coerce") if "date" in df else pd.Series(dtype="datetime64[ns]")
                schema_hash = hashlib.sha256(json.dumps(_schema(df), sort_keys=True).encode()).hexdigest()
                conn.execute("""INSERT INTO dataset_partitions
                    (dataset_name,partition_key,file_path,min_date,max_date,row_count,symbol_count,
                     file_size,schema_hash,checksum,status,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(dataset_name,partition_key) DO UPDATE SET
                    file_path=excluded.file_path,min_date=excluded.min_date,max_date=excluded.max_date,
                    row_count=excluded.row_count,symbol_count=excluded.symbol_count,file_size=excluded.file_size,
                    schema_hash=excluded.schema_hash,checksum=excluded.checksum,status=excluded.status,
                    updated_at=excluded.updated_at""",
                    ("stock_daily", path.stem, str(path), dates.min().date().isoformat() if dates.notna().any() else None,
                     dates.max().date().isoformat() if dates.notna().any() else None, len(df),
                     int(df["code"].astype(str).nunique()) if "code" in df else 0, path.stat().st_size,
                     schema_hash, _checksum(path), "legacy", _now()))
                count += 1
        return count

    def list_partitions(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM dataset_partitions WHERE dataset_name=? ORDER BY partition_key", ("stock_daily",)
            ).fetchall()]
