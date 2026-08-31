# -*- coding: utf-8 -*-
"""只读数据契约诊断测试（阶段一）。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from StockInvestmentTool.warehouse.contract_diagnostics import (
    coverage_alert,
    diagnose_all,
    diagnose_dataset,
)
from StockInvestmentTool.warehouse.pipeline_state import PipelineState


def _seed_management_db(db_path: Path, dataset_name: str, partition: str,
                        symbol_count: int, quality: str = "PASS",
                        publish: str = "published", path: str = "") -> None:
    state = PipelineState(db_path)
    version_id = f"v_{dataset_name}_{partition}"
    with __import__("sqlite3").connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO dataset_versions "
            "(version_id,dataset_name,partition_key,published_path,source_batches,row_count,"
            " symbol_count,min_date,max_date,schema_version,checksum,builder_version,"
            " quality_status,publish_status,created_at,published_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, dataset_name, partition, path, "[]", 100, symbol_count,
             "2026-08-01", "2026-08-31", "v1", "abc", "b.v1", quality, publish, "2026-08-31T00:00:00", "2026-08-31T00:00:00"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO dataset_current(dataset_name,partition_key,version_id,published_at) "
            "VALUES (?,?,?,?)", (dataset_name, partition, version_id, "2026-08-31T00:00:00"),
        )


def test_diagnose_dataset_reports_contract_facts(tmp_path):
    warehouse = tmp_path
    daily_dir = warehouse / "warehouse" / "daily"
    daily_dir.mkdir(parents=True)
    path = daily_dir / "2026-08.parquet"
    pd.DataFrame({
        "date": pd.to_datetime(["2026-08-27", "2026-08-28"]),
        "code": ["sh600000", "sz000001"],
        "close": [10.0, 12.0],
    }).to_parquet(path, index=False)
    db = tmp_path / "management.db"
    _seed_management_db(db, "stock_daily", "2026-08", symbol_count=2,
                        path=str(path))
    rows = diagnose_dataset(warehouse, "stock_daily", db)
    assert rows, "诊断应返回 stock_daily 分区"
    row = next(r for r in rows if r["partition"] == "2026-08")
    assert row["row_count"] == 2
    assert row["symbol_count"] == 2
    assert row["min_date"] == "2026-08-27"
    assert row["max_date"] == "2026-08-28"
    assert set(row["columns"]) >= {"date", "code", "close"}
    assert row["current_version"] == "v_stock_daily_2026-08"
    assert row["quality_status"] == "PASS"
    assert row["publish_status"] == "published"
    assert row["reachable"] is True


def test_diagnose_detects_unreachable_published_path(tmp_path):
    warehouse = tmp_path
    (warehouse / "warehouse" / "daily").mkdir(parents=True)
    db = tmp_path / "management.db"
    _seed_management_db(db, "stock_daily", "2026-08", symbol_count=100,
                        path=str(tmp_path / "missing.parquet"))
    rows = diagnose_dataset(warehouse, "stock_daily", db)
    row = next(r for r in rows if r["partition"] == "2026-08")
    assert row["published_path_reachable"] is False
    assert row["reachable"] is False


def test_diagnose_is_read_only(tmp_path):
    warehouse = tmp_path
    daily_dir = warehouse / "warehouse" / "daily"
    daily_dir.mkdir(parents=True)
    path = daily_dir / "2026-08.parquet"
    pd.DataFrame({
        "date": pd.to_datetime(["2026-08-28"]), "code": ["sh600000"], "close": [10.0],
    }).to_parquet(path, index=False)
    db = tmp_path / "management.db"
    _seed_management_db(db, "stock_daily", "2026-08", symbol_count=1, path=str(path))
    before = path.read_bytes()
    diagnose_dataset(warehouse, "stock_daily", db)
    assert path.read_bytes() == before, "诊断不得修改数据文件"
    assert db.stat().st_size > 0


def test_coverage_alert_flags_severe_low_coverage():
    rows = [
        {"dataset": "stock_daily", "partition": "2026-08", "symbol_count": 6800},
        {"dataset": "factors", "partition": "2026-08", "symbol_count": 8},
    ]
    alerts = coverage_alert([rows[1]], expected_symbols=6800)
    assert len(alerts) == 1
    assert alerts[0]["dataset"] == "factors"
    assert alerts[0]["coverage_ratio"] < 0.1


def test_coverage_alert_uses_dataset_benchmark_not_self():
    rows = [
        {"dataset": "factors", "partition": "2026-07", "symbol_count": 6800},
        {"dataset": "factors", "partition": "2026-08", "symbol_count": 8},
    ]
    alerts = coverage_alert(rows)
    assert len(alerts) == 1
    assert alerts[0]["partition"] == "2026-08"


def test_diagnose_all_covers_every_configured_dataset(tmp_path):
    warehouse = tmp_path
    (warehouse / "warehouse" / "daily").mkdir(parents=True)
    db = tmp_path / "management.db"
    report = diagnose_all(warehouse, db)
    names = set(report["datasets"])
    assert "stock_daily" in names
    assert "indicators" in names
    assert "valuation_daily" in names
    for name, rows in report["datasets"].items():
        assert isinstance(rows, list), f"{name} 诊断结果应为列表"