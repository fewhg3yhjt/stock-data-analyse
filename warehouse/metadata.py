"""Dataset metadata registry and partition indexing."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from StockInvestmentTool.warehouse.baseline import _schema, _checksum


SCHEMA_VERSION = "stock_daily.v1"
STOCK_DAILY_FIELDS = [
    ("date", "交易日期", "date", None, False, True, "业务交易日"),
    ("code", "证券代码", "string", None, False, True, "标准证券代码"),
    ("open", "开盘价", "float", "元", True, False, "停牌或来源缺失时单独报告"),
    ("high", "最高价", "float", "元", True, False, "停牌或来源缺失时单独报告"),
    ("low", "最低价", "float", "元", True, False, "停牌或来源缺失时单独报告"),
    ("close", "收盘价", "float", "元", False, False, "正常交易记录必须大于 0"),
    ("pre_close", "前收盘价", "float", "元", True, False, "用于涨跌计算和校验"),
    ("volume", "成交量", "float", "股", False, False, "停牌允许为 0，不允许负数"),
    ("amount", "成交额", "float", "元", False, False, "停牌允许为 0，不允许负数"),
    ("turn", "换手率", "float", "%", True, False, "来源缺失时允许为空"),
    ("tradestatus", "交易状态", "string/int", None, True, False, "用于停牌与缺口判断"),
]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class MetadataStore:
    """Idempotent metadata operations backed by the warehouse meta.db."""

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
        from StockInvestmentTool.warehouse.source_batches import SourceBatchStore
        SourceBatchStore(self.db_path)

    def register_stock_daily(self, storage_path: str = "warehouse/daily/YYYY-MM.parquet") -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute("""INSERT INTO dataset_registry
                (dataset_name,display_name,description,grain,primary_keys,partition_type,
                 storage_path,update_frequency,enabled,schema_version,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(dataset_name) DO UPDATE SET
                display_name=excluded.display_name, description=excluded.description,
                grain=excluded.grain, primary_keys=excluded.primary_keys,
                partition_type=excluded.partition_type, storage_path=excluded.storage_path,
                update_frequency=excluded.update_frequency, schema_version=excluded.schema_version,
                updated_at=excluded.updated_at""",
                ("stock_daily", "日线数据", "股票和 ETF 等证券每个交易日一条价格及成交事实记录",
                 "一个证券的一个交易日", json.dumps(["date", "code"]), "month",
                 storage_path, "交易日收盘后", 1, SCHEMA_VERSION, now, now))
            for field in STOCK_DAILY_FIELDS:
                name, display, dtype, unit, nullable, primary, description = field
                conn.execute("""INSERT INTO dataset_fields
                    (dataset_name,field_name,display_name,data_type,unit,nullable,is_primary_key,
                     description,schema_version,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(dataset_name,field_name) DO UPDATE SET
                    display_name=excluded.display_name,data_type=excluded.data_type,unit=excluded.unit,
                    nullable=excluded.nullable,is_primary_key=excluded.is_primary_key,
                    description=excluded.description,schema_version=excluded.schema_version,
                    updated_at=excluded.updated_at""",
                    ("stock_daily", name, display, dtype, unit, int(nullable), int(primary),
                     description, SCHEMA_VERSION, now, now))
            self._upsert_source(conn, "tencent", "primary", 1,
                                {field[0]: field[0] for field in STOCK_DAILY_FIELDS if field[0] != "pre_close"},
                                {"volume": "hand_to_share", "amount": "wan_yuan_to_yuan"})
            self._upsert_source(conn, "baostock", "validate + fallback", 2,
                                {field[0]: field[0] for field in STOCK_DAILY_FIELDS}, {})
            consumers = [
                ("market_discovery", "business", "全市场筛选", "PASS/WARNING", "禁止在线逐证券回退", "正式扫描"),
                ("indicators", "compute", "指标计算", "PASS/WARNING", "只读正式版本", "指标输入"),
                ("factors", "compute", "因子计算", "PASS/WARNING", "只读正式版本", "因子输入"),
                ("stock_research", "research", "K 线分析", "WARNING", "允许显式在线回退", "研究查看"),
                ("position_advice", "business", "正式决策", "PASS", "禁止静默回退", "建仓建议"),
            ]
            for name, kind, purpose, quality, fallback, blocked in consumers:
                conn.execute("""INSERT INTO dataset_consumers
                    (dataset_name,consumer_name,consumer_type,fields_used,purpose,required_quality,
                     fallback_policy,blocked_actions,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(dataset_name,consumer_name) DO UPDATE SET
                    consumer_type=excluded.consumer_type,purpose=excluded.purpose,
                    required_quality=excluded.required_quality,fallback_policy=excluded.fallback_policy,
                    blocked_actions=excluded.blocked_actions,updated_at=excluded.updated_at""",
                    ("stock_daily", name, kind, None, purpose, quality, fallback, blocked, now, now))

    @staticmethod
    def _upsert_source(conn, source, role, priority, mapping, conversions):
        now = _now()
        conn.execute("""INSERT INTO dataset_sources
            (dataset_name,source_name,role,priority,field_mapping,unit_conversions,
             request_defaults,enabled,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(dataset_name,source_name) DO UPDATE SET
            role=excluded.role,priority=excluded.priority,field_mapping=excluded.field_mapping,
            unit_conversions=excluded.unit_conversions,updated_at=excluded.updated_at""",
            ("stock_daily", source, role, priority, json.dumps(mapping),
             json.dumps(conversions), None, 1, now, now))

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
                    row_count=excluded.row_count,symbol_count=excluded.symbol_count,
                    file_size=excluded.file_size,schema_hash=excluded.schema_hash,
                    checksum=excluded.checksum,status=excluded.status,updated_at=excluded.updated_at""",
                    ("stock_daily", path.stem, str(path),
                     dates.min().date().isoformat() if dates.notna().any() else None,
                     dates.max().date().isoformat() if dates.notna().any() else None,
                     len(df), int(df["code"].astype(str).nunique()) if "code" in df else 0,
                     path.stat().st_size, schema_hash, _checksum(path), "legacy", _now()))
                count += 1
        return count

    def list_partitions(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM dataset_partitions WHERE dataset_name=? ORDER BY partition_key", ("stock_daily",)
            ).fetchall()]
