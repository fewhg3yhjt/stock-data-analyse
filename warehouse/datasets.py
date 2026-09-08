"""Unified reads from published dataset partitions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from StockInvestmentTool.warehouse.dataset_config import load_dataset_config


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
        partition_type = load_dataset_config(dataset_name)["dataset"]["partition"]["type"]
        months = self._months(start_date, end_date)
        versions = self.get_current_version(dataset_name)
        if partition_versions:
            versions = {month: {"version_id": version_id} for month, version_id in partition_versions.items()}
        if not versions:
            if not allow_legacy or dataset_name != "stock_daily":
                raise DatasetAccessError(f"{dataset_name} 没有 Published Dataset")
            if not months:
                months = self._local_months(dataset_name)
            return self._load_local_fallback(
                dataset_name, months, start_date, end_date, symbols,
                reason="没有 Published Dataset",
                quality_status="LEGACY" if allow_legacy else "WARNING",
            )
        if partition_type == "snapshot":
            if start_date or end_date:
                # Snapshot reads are as-of reads: use the newest snapshot that
                # was available by the requested end date.
                end = pd.Timestamp(end_date or start_date)
                available = [key for key in sorted(versions)
                             if pd.Timestamp(key) <= end]
                months = [available[-1]] if available else []
            else:
                months = sorted(versions)
        elif partition_type == "symbol":
            # Symbol-partitioned datasets (for example fundamentals) must not
            # scan every security file before applying the symbol filter.
            wanted = {str(value).lower().replace(".", "") for value in (symbols or [])}
            months = [key for key in sorted(versions) if not wanted or str(key).lower().replace(".", "") in wanted]
        elif not months:
            months = sorted(versions)
        frames = []
        contexts = {}
        for month in months:
            current = versions.get(month)
            path = None
            version = None
            quality = None
            fallback_reason = None
            if current:
                try:
                    version, quality = self._version_context(current["version_id"])
                    if version.get("publish_status") != "published":
                        fallback_reason = "版本不是 published 状态"
                    else:
                        path = Path(version["published_path"] or "")
                    if fallback_reason:
                        path = None
                    elif not path.exists():
                        path = None
                        fallback_reason = "正式文件不存在"
                    elif not self._quality_allowed(quality, required_quality):
                        fallback_reason = "正式版本质量不满足要求"
                    else:
                        import hashlib
                        if hashlib.sha256(path.read_bytes()).hexdigest() != version["checksum"]:
                            fallback_reason = "正式文件 checksum 不匹配"
                except DatasetAccessError as exc:
                    fallback_reason = str(exc)
            else:
                fallback_reason = "分区没有正式版本"

            if path is None:
                if not allow_legacy or dataset_name != "stock_daily":
                    raise DatasetAccessError(
                        f"{dataset_name}/{month} 没有可用的 Published 版本: {fallback_reason}"
                    )
                path = self._local_partition_path(dataset_name, month)
            if path is None or not path.exists():
                raise DatasetAccessError(
                    f"{dataset_name}/{month} 没有可读取的本地分区"
                )
            if fallback_reason and not (allow_legacy and dataset_name == "stock_daily"):
                raise DatasetAccessError(f"{dataset_name}/{month} Published 版本不可读: {fallback_reason}")
            if fallback_reason:
                # 治理信息保留用于诊断，但不阻断当前数据读取。
                import logging
                logging.getLogger(__name__).warning(
                    "%s/%s 读取降级: %s，直接读取本地分区 %s",
                    dataset_name, month, fallback_reason, path,
                )
            frame = pd.read_parquet(path)
            frames.append(frame)
            contexts[month] = {
                "version_id": version["version_id"] if version else "",
                "quality_status": (version or {}).get("quality_status", "WARNING"),
                "sources": json.loads((version or {}).get("source_batches", "[]") or "[]"),
                "input_versions": json.loads((version or {}).get("input_versions", "{}") or "{}"),
                "generated_at": (version or {}).get("created_at"),
            }
        data = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        filtered = self._filter(data, start_date, end_date, symbols)
        partition_versions = {k: v["version_id"] for k, v in versions.items() if k in months}
        quality_status = self._overall_quality(contexts)
        returned_start = self._min_date(filtered)
        returned_end = self._max_date(filtered)
        snapshot_dates = sorted({str(value)[:10] for value in filtered["snapshot_date"]}) if "snapshot_date" in filtered else []
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
            "snapshot_date": snapshot_dates[-1] if snapshot_dates else None,
            "snapshot_dates": snapshot_dates,
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

    def _local_partition_path(self, dataset_name: str, month: str) -> Path | None:
        """返回本地分区路径；文件存在即可读，不依赖治理索引。"""
        if dataset_name == "stock_daily":
            return self.warehouse.daily_partition(month)
        if dataset_name == "indicators":
            return self.warehouse.indicator_dir / f"{month}.parquet"
        if dataset_name == "industry_features_daily":
            return self.warehouse.industry_features_dir / f"{month}.parquet"
        return None

    def _local_months(self, dataset_name: str) -> list[str]:
        """列出本地已有分区，供治理索引缺失时读取。"""
        if dataset_name == "stock_daily":
            return self.warehouse.available_months("daily")
        if dataset_name == "indicators":
            return self.warehouse.available_months("indicator")
        if dataset_name == "industry_features_daily":
            return [p.stem for p in self.warehouse.industry_features_dir.glob("*.parquet")]
        return []

    def _load_local_fallback(self, dataset_name, months, start_date, end_date, symbols,
                             reason: str, quality_status: str = "WARNING"):
        """索引不可用时直接读本地分区，治理异常只记录不阻断。"""
        frames = []
        loaded_months = []
        for month in months:
            path = self._local_partition_path(dataset_name, month)
            if path is None or not path.exists():
                continue
            frames.append(pd.read_parquet(path))
            loaded_months.append(month)
        if not frames:
            raise DatasetAccessError(f"{reason}: {dataset_name}")
        import logging
        logging.getLogger(__name__).warning(
            "%s，直接读取本地分区: %s", reason, ",".join(loaded_months)
        )
        data = pd.concat(frames, ignore_index=True)
        filtered = self._filter(data, start_date, end_date, symbols)
        returned_start = self._min_date(filtered)
        returned_end = self._max_date(filtered)
        return DatasetResult(filtered, {
            "dataset": dataset_name,
            "dataset_refs": {dataset_name: {
                "partition_versions": {}, "quality_status": quality_status,
            }},
            "indicator_refs": {}, "partition_versions": {},
            "sources": [], "generated_at": None,
            "requested_start": start_date, "requested_end": end_date,
            "returned_start": returned_start, "returned_end": returned_end,
            "data_as_of": returned_end, "quality_status": quality_status,
            "source": "local_partition_fallback",
            "fallback_used": True, "fallback_reason": reason,
        })

    def _load_legacy(self, dataset_name, months, start_date, end_date, symbols):
        frames = []
        for month in months:
            path = self._local_partition_path(dataset_name, month)
            frames.append(pd.read_parquet(path) if path and path.exists() else None)
        frames = [frame for frame in frames if frame is not None and not frame.empty]
        if not frames:
            raise DatasetAccessError(f"没有可读取的本地分区: {dataset_name}")
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
        date_column = "date" if "date" in frame else "trading_date" if "trading_date" in frame else None
        if start_date and "snapshot_date" in frame:
            frame = frame[pd.to_datetime(frame["snapshot_date"]) >= pd.Timestamp(start_date)]
        if end_date and "snapshot_date" in frame:
            frame = frame[pd.to_datetime(frame["snapshot_date"]) <= pd.Timestamp(end_date)]
        if start_date and date_column:
            frame = frame[pd.to_datetime(frame[date_column]) >= pd.Timestamp(start_date)]
        if end_date and date_column:
            frame = frame[pd.to_datetime(frame[date_column]) <= pd.Timestamp(end_date)]
        if symbols and "code" in frame:
            frame = frame[frame["code"].astype(str).isin({str(code) for code in symbols})]
        return frame.reset_index(drop=True)

    @staticmethod
    def _max_date(frame):
        column = "date" if "date" in frame else "trading_date" if "trading_date" in frame else "snapshot_date" if "snapshot_date" in frame else None
        return str(pd.to_datetime(frame[column]).max())[:10] if not frame.empty and column else None

    @staticmethod
    def _min_date(frame):
        column = "date" if "date" in frame else "trading_date" if "trading_date" in frame else "snapshot_date" if "snapshot_date" in frame else None
        return str(pd.to_datetime(frame[column]).min())[:10] if not frame.empty and column else None

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
