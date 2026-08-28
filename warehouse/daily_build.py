"""Build a candidate stock_daily partition from immutable Raw batches."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from StockInvestmentTool.warehouse.dataset_config import load_dataset_config


class DailyBuilder:
    def __init__(self, warehouse, config_path: Path | str | None = None):
        self.warehouse = warehouse
        self.config = load_dataset_config("stock_daily", config_path)

    def _normalize(self, frame: pd.DataFrame, source: str) -> pd.DataFrame:
        mapping = next(item for item in self.config["sources"] if item["name"] == source).get("field_mapping", {})
        out = frame.rename(columns={raw: standard for standard, raw in mapping.items()}).copy()
        out["date"] = pd.to_datetime(out["date"])
        out["code"] = out["code"].astype(str).str.lower().str.replace(".", "", regex=False)
        conversions = next(item for item in self.config["sources"] if item["name"] == source).get("unit_conversions", {})
        if conversions.get("volume") == "hand_to_share" and "volume" in out:
            out["volume"] = pd.to_numeric(out["volume"], errors="coerce") * 100
        if conversions.get("amount") == "wan_yuan_to_yuan" and "amount" in out:
            out["amount"] = pd.to_numeric(out["amount"], errors="coerce") * 10000
        for field in self.config["fields"]:
            if field["name"] not in out:
                out[field["name"]] = pd.NA
        return out[[field["name"] for field in self.config["fields"]]]

    def build_partition(self, partition: str, raw_batches: Iterable[tuple[str, Path]],
                        *, include_current: bool = True) -> dict:
        frames = []
        source_frames = {}
        for source, path in raw_batches:
            frame = self._normalize(pd.read_parquet(path), source)
            frame["_source"] = source
            frames.append(frame)
            source_frames[source] = frame
        if include_current:
            current = self.warehouse.read_daily(partition)
            if current is not None and not current.empty:
                frames.append(self._normalize(current, "baostock" if "pre_close" in current else "tencent"))
        if not frames:
            raise ValueError("没有可用于构建的 Raw Batch")
        combined = pd.concat(frames, ignore_index=True)
        conflicts = self._conflicts(source_frames)
        # Source priority is declared in YAML: lower priority number wins.
        priority = {item["name"]: item["priority"] for item in self.config["sources"]}
        combined["_priority"] = combined["_source"].map(priority).fillna(999)
        # Lower priority numbers win. The original row order breaks ties.
        selected = []
        for key, group in combined.groupby(["date", "code"], sort=False):
            group = group.sort_values("_priority", kind="stable")
            selected.append(group.iloc[0])
        result = pd.DataFrame(selected).drop(columns=["_priority", "_source"], errors="ignore")
        result = result.drop_duplicates(["date", "code"]).sort_values(["date", "code"]).reset_index(drop=True)
        candidate_root = self.warehouse.base_dir / "candidates" / "stock_daily" / partition
        candidate_root.mkdir(parents=True, exist_ok=True)
        version_id = f"stock_daily_{partition.replace('-', '')}_{uuid.uuid4().hex[:10]}"
        path = candidate_root / f"{version_id}.parquet"
        result.to_parquet(path, index=False, engine="pyarrow", compression="zstd")
        return {"version_id": version_id, "partition": partition, "path": path,
                "row_count": len(result), "symbol_count": result["code"].nunique(),
                "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
                "source_conflicts": conflicts, "source_batches": [str(path) for _, path in raw_batches]}

    @staticmethod
    def _conflicts(source_frames: dict[str, pd.DataFrame]) -> list[dict]:
        if len(source_frames) < 2:
            return []
        names = list(source_frames)
        left, right = source_frames[names[0]], source_frames[names[1]]
        keys = ["date", "code"]
        fields = [field for field in ("open", "high", "low", "close", "volume", "amount") if field in left and field in right]
        joined = left.set_index(keys).join(right.set_index(keys), lsuffix="_left", rsuffix="_right", how="inner")
        conflicts = []
        for key, row in joined.iterrows():
            for field in fields:
                a, b = row.get(f"{field}_left"), row.get(f"{field}_right")
                if pd.notna(a) and pd.notna(b) and float(a) != float(b):
                    conflicts.append({"date": str(key[0])[:10], "code": key[1], "field": field})
        return conflicts
