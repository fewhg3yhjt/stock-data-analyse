# -*- coding: utf-8 -*-
"""只读数据契约诊断：不修改生产数据库或文件。

输出每个数据集在每个分区的契约事实，用于识别低覆盖、日期滞后、
 Published 路径不可达等问题。诊断只读 management.db 与显式迁移输入库，
Parquet 文件元数据，不做任何写操作。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pandas as pd

from StockInvestmentTool.warehouse.baseline import _checksum, _schema
from StockInvestmentTool.warehouse.dataset_config import load_dataset_config


def _digest(path: Path) -> str:
    try:
        return _checksum(path)
    except OSError:
        return ""


def _partition_summary(path: Path, partition: str) -> dict:
    result = {
        "partition": partition,
        "path": str(path),
        "row_count": 0,
        "symbol_count": 0,
        "min_date": None,
        "max_date": None,
        "columns": [],
        "reachable": bool(path.exists()),
        "file_size": 0,
    }
    if not path.exists():
        return result
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        result["read_error"] = str(exc)
        return result
    result["row_count"] = int(len(df))
    result["columns"] = list(df.columns)
    result["file_size"] = path.stat().st_size
    if "code" in df.columns:
        result["symbol_count"] = int(df["code"].astype(str).nunique())
    date_col = "date" if "date" in df.columns else "stat_date" if "stat_date" in df.columns else None
    if date_col and len(df):
        values = pd.to_datetime(df[date_col], errors="coerce")
        if values.notna().any():
            result["min_date"] = str(values.min())[:10]
            result["max_date"] = str(values.max())[:10]
    return result


def _database_versions(db_path: Path, dataset_name: str) -> dict:
    """Return {partition: row} from dataset_versions, using latest by created_at."""
    versions = {}
    if not Path(db_path).exists():
        return versions
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT version_id,partition_key,publish_status,quality_status,"
                "candidate_path,published_path,checksum,row_count,symbol_count,min_date,max_date "
                "FROM dataset_versions WHERE dataset_name=? "
                "ORDER BY created_at DESC", (dataset_name,)
            ).fetchall()
    except sqlite3.Error:
        return versions
    for row in rows:
        partition = row["partition_key"]
        if partition not in versions:
            versions[partition] = dict(row)
    return versions


def _current_versions(db_path: Path) -> dict:
    current = {}
    if not Path(db_path).exists():
        return current
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT dataset_name,partition_key,version_id FROM dataset_current").fetchall()
    except sqlite3.Error:
        return current
    for row in rows:
        current.setdefault(row["dataset_name"], {})[row["partition_key"]] = row["version_id"]
    return current


def _resolve_template(template: str, warehouse_root: Path, partition: str | None = None) -> Path:
    """解析 partition path 模板（如 warehouse/daily/{partition}.parquet）为绝对路径。"""
    if partition is not None:
        try:
            return Path(warehouse_root) / template.format(partition=partition)
        except (KeyError, IndexError):
            return Path(warehouse_root) / template
    return Path(warehouse_root) / template.replace("{partition}", "")


def diagnose_dataset(warehouse_root: Path, dataset_name: str, db_path: Path,
                     partition_filter: str | None = None) -> list[dict]:
    """诊断一个数据集的契约事实（只读）。"""
    config = load_dataset_config(dataset_name)
    storage_path = config["dataset"]["partition"]["path"]  # 相对 Warehouse Root 的模板
    data_dir = _resolve_template(storage_path, warehouse_root)
    versions = _database_versions(db_path, dataset_name)
    current_map = _current_versions(db_path)

    files = {}
    if data_dir.is_dir():
        for path in sorted(data_dir.glob("*.parquet")):
            files[path.stem] = path
        if not files:
            template = storage_path.replace("{partition}", "{}")
            try:
                parent = Path(warehouse_root) / str(Path(template).parent)
            except ValueError:
                parent = data_dir
            if parent.is_dir():
                for path in sorted(parent.glob("*.parquet")):
                    files[path.stem] = path

    all_partitions = set(files) | set(versions)
    partitions = []
    for partition in sorted(all_partitions):
        if partition_filter and partition != partition_filter:
            continue
        file_path = files.get(partition)
        if file_path is None:
            file_path = _resolve_template(storage_path, warehouse_root, partition)
        summary = _partition_summary(file_path, partition) if file_path.exists() else {
            "partition": partition, "path": str(file_path),
            "row_count": 0, "symbol_count": 0, "min_date": None, "max_date": None,
            "columns": [], "reachable": False, "file_size": 0,
        }
        version = versions.get(partition)
        current_version = current_map.get(dataset_name, {}).get(partition)
        checksum_match = None
        if version and file_path and version.get("checksum"):
            checksum_match = _digest(file_path) == version["checksum"]
        row = {
            "dataset": dataset_name,
            "partition": partition,
            "row_count": summary["row_count"],
            "symbol_count": summary["symbol_count"],
            "min_date": summary["min_date"],
            "max_date": summary["max_date"],
            "columns": summary["columns"],
            "reachable": summary["reachable"],
            "current_version": current_version,
            "quality_status": version.get("quality_status") if version else None,
            "publish_status": version.get("publish_status") if version else None,
            "checksum_match": checksum_match,
            "db_published_path": version.get("published_path") if version else None,
            "db_candidate_path": version.get("candidate_path") if version else None,
        }
        if not summary["reachable"] and version:
            row["published_path_reachable"] = _is_path_reachable(version.get("published_path"))
        partitions.append(row)
    return partitions


def _is_path_reachable(path: str | None) -> bool:
    if not path:
        return False
    return Path(path).exists()


def diagnose_all(warehouse_root: Path, db_path: Path,
                 dataset_filter: str | None = None,
                 partition_filter: str | None = None) -> dict:
    """诊断 config/datasets/*.yaml 定义的全部数据集（只读）。"""
    from pathlib import Path as _P
    config_dir = _P(__file__).resolve().parents[1] / "config" / "datasets"
    names = [path.stem for path in sorted(config_dir.glob("*.yaml"))
             if dataset_filter is None or path.stem == dataset_filter]
    results = {}
    for name in names:
        try:
            results[name] = diagnose_dataset(warehouse_root, name, db_path, partition_filter)
        except Exception as exc:
            results[name] = [{"dataset": name, "error": str(exc)}]
    return {"datasets": results}


def coverage_alert(partitions: list[dict], *, expected_symbols: int | None = None,
                   coverage_threshold: float = 0.1) -> list[dict]:
    """识别严重低覆盖分区（只读判断，不做健康判定）。

    当某个分区实际 symbol_count 远小于基准（默认仅达到 10% 以下）时返回告警。
    expected_symbols 缺省时使用同一数据集其余分区的最大 symbol_count 作为代理基准，
    避免依赖"文件自身自证"。
    """
    alerts = []
    if not partitions:
        return alerts
    populated = [p for p in partitions if p.get("symbol_count")]
    if not populated:
        return alerts
    if expected_symbols is None:
        expected_symbols = max(p["symbol_count"] for p in populated)
    for p in partitions:
        count = p.get("symbol_count") or 0
        if expected_symbols and count / expected_symbols < coverage_threshold:
            alerts.append({
                "dataset": p["dataset"], "partition": p["partition"],
                "symbol_count": count, "expected_symbols": expected_symbols,
                "coverage_ratio": round(count / expected_symbols, 4),
                "detail": "严重低覆盖：与数据集基准相比覆盖不足 10%",
            })
    return alerts


def write_report(report: dict, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path
