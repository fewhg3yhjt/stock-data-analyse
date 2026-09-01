"""Build deterministic stock_daily candidates from immutable Raw Batches."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from StockInvestmentTool.warehouse.dataset_config import load_dataset_config


class DailyBuilder:
    def __init__(self, warehouse, config_path: Path | str | None = None):
        self.warehouse = warehouse
        self.config = load_dataset_config("stock_daily", config_path)
        self._sources = {item["name"]: item for item in self.config["sources"]}

    def select_raw_batches(self, partition: str, sources: Optional[list[str]] = None) -> list[tuple[str, Path, str]]:
        """Select completed, existing source batches overlapping a month."""
        month_start = pd.Timestamp(f"{partition}-01")
        month_end = month_start + pd.offsets.MonthEnd(1)
        with self.warehouse._conn() as conn:
            rows = conn.execute(
                "SELECT batch_id, source_name, raw_path, trade_date_start, trade_date_end "
                "FROM source_batches WHERE dataset_name='stock_daily' "
                "AND status IN ('success','partial_success') AND raw_path IS NOT NULL "
                "ORDER BY finished_at DESC, batch_id DESC"
            ).fetchall()
        selected = []
        for batch_id, source, raw_path, date_start, date_end in rows:
            if sources and source not in sources:
                continue
            path = Path(raw_path)
            if source not in self._sources or not path.exists():
                continue
            batch_start = pd.Timestamp(date_start) if date_start else month_start
            batch_end = pd.Timestamp(date_end) if date_end else month_end
            if batch_start <= month_end and batch_end >= month_start:
                selected.append((source, path, batch_id))
        if not selected:
            raise ValueError(f"没有找到 {partition} 可用的 stock_daily Raw Batch")
        return selected

    def _normalize(self, frame: pd.DataFrame, source: str) -> pd.DataFrame:
        if source not in self._sources:
            raise ValueError(f"未注册的数据源: {source}")
        source_config = self._sources[source]
        mapping = source_config.get("field_mapping", {})
        out = frame.rename(columns={raw: standard for standard, raw in mapping.items()}).copy()
        required = {"date", "code"}
        if not required.issubset(out.columns):
            raise ValueError(f"{source} Raw 缺少主键字段: {sorted(required - set(out.columns))}")
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out["code"] = out["code"].astype(str).str.lower().str.replace(".", "", regex=False)
        conversions = source_config.get("unit_conversions", {})
        if conversions.get("volume") == "hand_to_share" and "volume" in out:
            out["volume"] = pd.to_numeric(out["volume"], errors="coerce") * 100
        if conversions.get("amount") == "wan_yuan_to_yuan" and "amount" in out:
            out["amount"] = pd.to_numeric(out["amount"], errors="coerce") * 10000
        for field in self.config["fields"]:
            if field["name"] not in out:
                out[field["name"]] = pd.NA
        return out[[field["name"] for field in self.config["fields"]]]

    def _normalize_current(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        for field in self.config["fields"]:
            if field["name"] not in out:
                out[field["name"]] = pd.NA
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out["code"] = out["code"].astype(str).str.lower().str.replace(".", "", regex=False)
        return out[[field["name"] for field in self.config["fields"]]]

    def _read_raw_partition(self, path: Path, source: str, partition: str) -> pd.DataFrame:
        """Read only one month from a raw batch without materializing the batch.

        Raw batches may contain several years of data.  Reading the complete
        parquet file for every monthly build can exceed the production
        container memory limit, so process bounded Arrow batches instead.
        """
        import pyarrow.parquet as pq

        month_start = pd.Timestamp(f"{partition}-01")
        month_end = month_start + pd.offsets.MonthEnd(1)
        chunks = []
        parquet = pq.ParquetFile(path)
        for record_batch in parquet.iter_batches(batch_size=50_000):
            frame = self._normalize(record_batch.to_pandas(), source)
            dates = pd.to_datetime(frame["date"], errors="coerce")
            frame = frame[(dates >= month_start) & (dates <= month_end)]
            if not frame.empty:
                chunks.append(frame)
        if not chunks:
            return pd.DataFrame(columns=[field["name"] for field in self.config["fields"]])
        return pd.concat(chunks, ignore_index=True)

    def build_partition(self, partition: str, raw_batches: Optional[Iterable[tuple]] = None,
                        *, include_current: bool = True) -> dict:
        selected_batches = self.select_raw_batches(partition) if raw_batches is None else list(raw_batches)
        frames = []
        source_frames: dict[str, pd.DataFrame] = {}
        batch_ids = []
        for item in selected_batches:
            source, path = item[:2]
            batch_ids.append(item[2] if len(item) > 2 else str(path))
            frame = self._read_raw_partition(path, source, partition)
            if frame is not None and not frame.empty:
                previous = source_frames.get(source)
                source_frames[source] = (pd.concat([previous, frame], ignore_index=True)
                                         if previous is not None and not previous.empty else frame)
                frame["_source"] = source
                frames.append(frame)
        if include_current:
            current = self.warehouse.read_daily(partition)
            if current is not None and not current.empty:
                current_frame = self._normalize_current(current)
                current_frame["_source"] = "legacy_daily"
                frames.append(current_frame)
        if not frames:
            raise ValueError("没有可用于构建的 Raw Batch")

        combined = pd.concat(frames, ignore_index=True)
        combined["date"] = pd.to_datetime(combined["date"], errors="coerce")
        priority = {name: item["priority"] for name, item in self._sources.items()}
        priority["legacy_daily"] = 999
        combined["_priority"] = combined["_source"].map(priority).fillna(999)
        combined = combined.sort_values(["date", "code", "_priority"], kind="stable")
        result = combined.drop_duplicates(["date", "code"], keep="first")
        result = result.drop(columns=["_priority", "_source"], errors="ignore")
        result = result.drop_duplicates(["date", "code"]).sort_values(["date", "code"]).reset_index(drop=True)
        result = result[result["date"].dt.strftime("%Y-%m") == partition].reset_index(drop=True)

        canonical = result.to_json(date_format="iso", orient="records")
        content_fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        version_id = f"stock_daily_{partition.replace('-', '')}_{content_fingerprint[:12]}"
        candidate_root = self.warehouse.base_dir / "candidates" / "stock_daily" / partition
        candidate_root.mkdir(parents=True, exist_ok=True)
        path = candidate_root / f"{version_id}.parquet"
        if not path.exists():
            result.to_parquet(path, index=False, engine="pyarrow", compression="zstd")
        return {
            "version_id": version_id, "partition": partition, "path": path,
            "row_count": len(result), "symbol_count": int(result["code"].nunique()),
            "min_date": str(result["date"].min())[:10] if not result.empty else None,
            "max_date": str(result["date"].max())[:10] if not result.empty else None,
            "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_conflicts": self._conflicts(source_frames),
            "source_batches": batch_ids, "input_fingerprint": content_fingerprint,
            "diff_report": self._diff_current(partition, result) if include_current else None,
        }

    def _diff_current(self, partition: str, candidate: pd.DataFrame) -> dict:
        current = self.warehouse.read_daily(partition)
        if current is None or current.empty:
            return {"current_rows": 0, "candidate_rows": len(candidate), "added_keys": len(candidate),
                    "removed_keys": 0, "field_differences": 0}
        left = self._normalize_current(current).set_index(["date", "code"])
        right = candidate.set_index(["date", "code"])
        common = left.index.intersection(right.index)
        fields = [field for field in ("open", "high", "low", "close", "volume", "amount") if field in left and field in right]
        differences = 0
        for field in fields:
            a = pd.to_numeric(left.loc[common, field], errors="coerce")
            b = pd.to_numeric(right.loc[common, field], errors="coerce")
            differences += int((a.sub(b).abs().fillna(0) > 1e-9).sum())
        return {"current_rows": len(left), "candidate_rows": len(right),
                "added_keys": len(right.index.difference(left.index)),
                "removed_keys": len(left.index.difference(right.index)),
                "field_differences": differences, "common_keys": len(common), "fields": fields}

    @staticmethod
    def _conflicts(source_frames: dict[str, pd.DataFrame]) -> list[dict]:
        names = list(source_frames)
        conflicts = []
        for index, left_name in enumerate(names):
            for right_name in names[index + 1:]:
                left = source_frames[left_name]
                right = source_frames[right_name]
                fields = [field for field in ("open", "high", "low", "close", "volume", "amount") if field in left and field in right]
                if not fields:
                    continue
                joined = left.set_index(["date", "code"]).join(
                    right.set_index(["date", "code"]), lsuffix="_left", rsuffix="_right", how="inner"
                )
                for key, row in joined.iterrows():
                    for field in fields:
                        a, b = row.get(f"{field}_left"), row.get(f"{field}_right")
                        if pd.notna(a) and pd.notna(b) and float(a) != float(b):
                            conflicts.append({"left_source": left_name, "right_source": right_name,
                                              "date": str(key[0])[:10], "code": key[1], "field": field})
        return conflicts
