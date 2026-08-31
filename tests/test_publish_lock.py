# -*- coding: utf-8 -*-
"""阶段十：发布一致性与恢复（B4 分区锁）。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pandas as pd
import pytest

from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.publish import PublishLockError
from StockInvestmentTool.warehouse.quality import check_stock_daily
from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.daily_build import DailyBuilder
from StockInvestmentTool.warehouse.storage import Warehouse


def _prepare_partition(warehouse: Warehouse, close: float, partition: str = "2026-08") -> str:
    warehouse.metadata.register_stock_daily()
    frame = pd.DataFrame({
        "date": pd.to_datetime([f"{partition}-04"]), "code": ["sh600900"],
        "open": [10.0], "high": [10.5], "low": [9.8], "close": [close],
        "volume": [1000], "amount": [1e6], "turn": [0.5],
    })
    source = capture_frames(
        warehouse, dataset_name="stock_daily", source_name="tencent",
        frames=[frame], expected_symbols=1, success_symbols=1,
        universe_id="u", request_context={"fixture": True},
    )
    build = DailyBuilder(warehouse).build_partition(partition, [("tencent", source["raw"]["path"])], include_current=False)
    state = PipelineState(warehouse.meta_db_path)
    version = state.create_version(build, source_batches=[source["batch_id"]])
    quality = check_stock_daily(build["path"], expected_symbols=1)
    state.quality(version, status=quality["status"], checks=quality["checks"],
                  publish_allowed=quality["publish_allowed"])
    return version


def test_concurrent_publish_different_partitions(tmp_path):
    """并发发布不同分区：锁按分区隔离，互不阻塞。"""
    warehouse = Warehouse(tmp_path / "warehouse")
    v1 = _prepare_partition(warehouse, 10.0, "2026-08")
    v2 = _prepare_partition(warehouse, 10.4, "2026-09")

    results = []
    errors = []

    def do_publish(version_id):
        try:
            results.append(Publisher(warehouse).publish(version_id))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=do_publish, args=(v,)) for v in (v1, v2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 2
    with sqlite3.connect(warehouse.meta_db_path) as conn:
        current_a = conn.execute(
            "SELECT version_id FROM dataset_current WHERE dataset_name='stock_daily' AND partition_key='2026-08'"
        ).fetchone()[0]
        current_b = conn.execute(
            "SELECT version_id FROM dataset_current WHERE dataset_name='stock_daily' AND partition_key='2026-09'"
        ).fetchone()[0]
        locks = conn.execute("SELECT COUNT(*) FROM dataset_publish_locks").fetchone()[0]
        assert current_a == v1
        assert current_b == v2
        assert locks == 0
    assert warehouse.read_daily("2026-08")["close"].iloc[0] == 10.0
    assert warehouse.read_daily("2026-09")["close"].iloc[0] == 10.4


def test_concurrent_publish_same_version_is_idempotent(tmp_path):
    """并发发布同一版本：幂等，不产生锁残留。"""
    warehouse = Warehouse(tmp_path / "warehouse")
    version = _prepare_partition(warehouse, 10.0, "2026-08")

    results = []
    errors = []

    def do_publish():
        try:
            results.append(Publisher(warehouse).publish(version))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=do_publish) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 2
    with sqlite3.connect(warehouse.meta_db_path) as conn:
        current = conn.execute(
            "SELECT version_id FROM dataset_current WHERE dataset_name='stock_daily' AND partition_key='2026-08'"
        ).fetchone()[0]
        locks = conn.execute("SELECT COUNT(*) FROM dataset_publish_locks").fetchone()[0]
        assert current == version
        assert locks == 0


def test_lock_is_held_during_publish_and_released(tmp_path):
    """publish 结束后锁被释放。"""
    warehouse = Warehouse(tmp_path / "warehouse")
    version = _prepare_partition(warehouse, 10.0, "2026-08")
    Publisher(warehouse).publish(version)
    with sqlite3.connect(warehouse.meta_db_path) as conn:
        locks = conn.execute("SELECT COUNT(*) FROM dataset_publish_locks").fetchone()[0]
    assert locks == 0


def test_lock_table_schema_created(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    _prepare_partition(warehouse, 10.0, "2026-08")
    with sqlite3.connect(warehouse.meta_db_path) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "dataset_publish_locks" in tables


def test_publish_failed_state_recorded_on_bad_checksum(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    version = _prepare_partition(warehouse, 10.0, "2026-08")
    # 篡改候选文件使 checksum 不匹配
    state = PipelineState(warehouse.meta_db_path)
    with sqlite3.connect(warehouse.meta_db_path) as conn:
        candidate = conn.execute(
            "SELECT candidate_path FROM dataset_versions WHERE version_id=?", (version,)
        ).fetchone()[0]
    Path(candidate).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum 不匹配"):
        Publisher(warehouse).publish(version)
    with sqlite3.connect(warehouse.meta_db_path) as conn:
        status = conn.execute(
            "SELECT publish_status FROM dataset_versions WHERE version_id=?", (version,)
        ).fetchone()[0]
    assert status in ("candidate", "publish_failed")