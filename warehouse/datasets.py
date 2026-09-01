"""Unified reads from published dataset partitions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd


@dataclass
class DatasetResult:
    data: pd.DataFrame
    context: dict


class DatasetAccessError(RuntimeError):
    pass


class DatasetAccess:
    def __init__(self, warehouse):
        self.warehouse = warehouse

    def get_current_version(self, dataset_name: str, partition: str | None = None):
        with sqlite3.connect(self.warehouse.meta_db_path) as conn:
            try:
                if partition:
                    row = conn.execute(
                        "SELECT version_id, published_at FROM dataset_current WHERE dataset_name=? AND partition_key=?",
                        (dataset_name, partition),
                    ).fetchone()
                    return {"version_id": row[0], "published_at": row[1]} if row else None
                rows = conn.execute(
                    "SELECT partition_key, version_id, published_at FROM dataset_current WHERE dataset_name=? ORDER BY partition_key",
                    (dataset_name,),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return None if partition else {}
                raise
        return {row[0]: {"version_id": row[1], "published_at": row[2]} for row in rows}

    def load_dataset(self, dataset_name: str, start_date: Optional[str] = None,
                     end_date: Optional[str] = None, symbols: Optional[list[str]] = None,
                     required_quality: str = "WARNING", *, allow_legacy: bool = False,
                     partition_versions: dict[str, str] | None = None) -> DatasetResult:
        months = self._months(start_date, end_date)
        versions = self.get_current_version(dataset_name)
        if partition_versions:
            versions = {month: {"version_id": version_id} for month, version_id in partition_versions.items()}
        if not versions:
            if not allow_legacy:
                raise DatasetAccessError(f"没有 Published Dataset: {dataset_name}")
            return self._load_legacy(dataset_name, months, start_date, end_date, symbols)
        if not months:
            months = sorted(versions)
        frames = []
        contexts = {}
        for month in months:
            current = versions.get(month)
            if not current:
                raise DatasetAccessError(f"分区没有正式版本: {dataset_name}/{month}")
            version, quality = self._version_context(current["version_id"])
            if not self._quality_allowed(quality, required_quality):
                raise DatasetAccessError(f"正式版本质量不满足要求: {dataset_name}/{month}")
            path = Path(version["published_path"] or "")
            if not path.exists():
                raise DatasetAccessError(f"正式文件不存在: {path}")
            import hashlib
            if hashlib.sha256(path.read_bytes()).hexdigest() != version["checksum"]:
                raise DatasetAccessError(f"正式文件 checksum 不匹配: {path}")
            frame = pd.read_parquet(path)
            frames.append(frame)
            contexts[month] = {"version_id": version["version_id"],
                               "quality_status": version["quality_status"],
                               "sources": json.loads(version["source_batches"] or "[]"),
                               "input_versions": json.loads(version["input_versions"] or "{}"),
                               "generated_at": version["created_at"]}
        data = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        filtered = self._filter(data, start_date, end_date, symbols)
        partition_versions = {k: v["version_id"] for k, v in versions.items() if k in months}
        quality_status = self._overall_quality(contexts)
        returned_start = self._min_date(filtered)
        returned_end = self._max_date(filtered)
        return DatasetResult(filtered, {
            "dataset": dataset_name,
            "dataset_refs": {dataset_name: {
                "partition_versions": partition_versions,
                "quality_status": quality_status,
            }},
            "indicator_refs": {},
            "partition_versions": partition_versions,
            "partitions": contexts,
            "max_date": returned_end,
            "requested_start": start_date,
            "requested_end": end_date,
            "returned_start": returned_start,
            "returned_end": returned_end,
            "data_as_of": returned_end,
            "quality_status": quality_status,
            "source": "published_dataset",
            "fallback_used": False,
            "fallback_reason": None,
        })

    def get_dataset_context(self, dataset_name: str, start_date=None, end_date=None) -> dict:
        return self.load_dataset(dataset_name, start_date, end_date).context

    def _version_context(self, version_id):
        with sqlite3.connect(self.warehouse.meta_db_path) as conn:
            conn.row_factory = sqlite3.Row
            version = conn.execute("SELECT * FROM dataset_versions WHERE version_id=?", (version_id,)).fetchone()
            quality = conn.execute("SELECT * FROM dataset_quality_results WHERE version_id=? ORDER BY checked_at DESC LIMIT 1", (version_id,)).fetchone()
        if version is None:
            raise DatasetAccessError(f"版本记录不存在: {version_id}")
        return dict(version), (dict(quality) if quality else None)

    @staticmethod
    def _quality_allowed(quality, required):
        if quality is None:
            return False
        rank = {"PASS": 2, "WARNING": 1, "FAIL": 0}
        return rank.get(quality["status"], -1) >= rank.get(required, 1)

    def _load_legacy(self, dataset_name, months, start_date, end_date, symbols):
        if dataset_name != "stock_daily":
            raise DatasetAccessError(f"不支持旧路径兼容读取: {dataset_name}")
        frames = [self.warehouse.read_daily(month) for month in months]
        frames = [frame for frame in frames if frame is not None and not frame.empty]
        data = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        filtered = self._filter(data, start_date, end_date, symbols)
        returned_start = self._min_date(filtered)
        returned_end = self._max_date(filtered)
        return DatasetResult(filtered, {
            "dataset": dataset_name,
            "dataset_refs": {dataset_name: {"partition_versions": {}, "quality_status": "LEGACY"}},
            "indicator_refs": {},
            "partition_versions": {},
            "sources": [],
            "generated_at": None,
            "requested_start": start_date,
            "requested_end": end_date,
            "returned_start": returned_start,
            "returned_end": returned_end,
            "data_as_of": returned_end,
            "quality_status": "LEGACY",
            "source": "legacy_dataset",
            "fallback_used": True,
            "fallback_reason": "published_dataset_unavailable",
        })

    @staticmethod
    def _months(start_date, end_date):
        if not start_date and not end_date:
            return []
        start = pd.Timestamp(start_date or end_date).replace(day=1)
        end = pd.Timestamp(end_date or start_date).replace(day=1)
        return [str(item.strftime("%Y-%m")) for item in pd.date_range(start, end, freq="MS")]

    @staticmethod
    def _filter(frame, start_date, end_date, symbols):
        if frame.empty:
            return frame
        if start_date and "date" in frame:
            frame = frame[pd.to_datetime(frame["date"]) >= pd.Timestamp(start_date)]
        if end_date and "date" in frame:
            frame = frame[pd.to_datetime(frame["date"]) <= pd.Timestamp(end_date)]
        if symbols and "code" in frame:
            frame = frame[frame["code"].astype(str).isin({str(code) for code in symbols})]
        return frame.reset_index(drop=True)

    @staticmethod
    def _max_date(frame):
        return str(pd.to_datetime(frame["date"]).max())[:10] if not frame.empty and "date" in frame else None

    @staticmethod
    def _min_date(frame):
        return str(pd.to_datetime(frame["date"]).min())[:10] if not frame.empty and "date" in frame else None

    @staticmethod
    def _overall_quality(contexts):
        statuses = {item["quality_status"] for item in contexts.values()}
        return "FAIL" if "FAIL" in statuses else ("WARNING" if "WARNING" in statuses else "PASS")


def load_dataset(warehouse, dataset_name: str, start_date=None, end_date=None,
                 symbols=None, required_quality="WARNING", *, allow_legacy=False,
                 partition_versions=None):
    return DatasetAccess(warehouse).load_dataset(dataset_name, start_date, end_date, symbols,
                                                  required_quality, allow_legacy=allow_legacy,
                                                  partition_versions=partition_versions)
