"""Generic YAML-driven builder for non-derived source datasets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from StockInvestmentTool.warehouse.dataset_config import load_dataset_config


class DatasetBuilder:
    def __init__(self, warehouse, dataset_name: str, config_path=None):
        self.warehouse = warehouse
        self.dataset_name = dataset_name
        self.config = load_dataset_config(dataset_name, config_path)

    def build(self, partition: str, raw_paths: list[tuple[str, Path]], *, key_columns=None) -> dict:
        frames = []
        for source, path in raw_paths:
            source_config = next(item for item in self.config["sources"] if item["name"] == source)
            mapping = source_config.get("field_mapping", {})
            frame = pd.read_parquet(path).rename(columns={raw: standard for standard, raw in mapping.items()})
            for field in self.config["fields"]:
                if field["name"] not in frame:
                    frame[field["name"]] = pd.NA
            frames.append(frame[[field["name"] for field in self.config["fields"]]])
        if not frames:
            raise ValueError(f"没有可构建的 {self.dataset_name} Raw 数据")
        result = pd.concat(frames, ignore_index=True)
        keys = key_columns or self.config["dataset"]["primary_keys"]
        result = result.drop_duplicates(keys, keep="last").sort_values(keys).reset_index(drop=True)
        candidate_root = self.warehouse.base_dir / "candidates" / self.dataset_name / partition
        candidate_root.mkdir(parents=True, exist_ok=True)
        fingerprint = hashlib.sha256(result.to_json(orient="records", date_format="iso").encode()).hexdigest()
        version_id = f"{self.dataset_name}_{partition.replace('-', '')}_{fingerprint[:12]}"
        path = candidate_root / f"{version_id}.parquet"
        if not path.exists():
            result.to_parquet(path, index=False, engine="pyarrow", compression="zstd")
        return {"version_id": version_id, "dataset_name": self.dataset_name, "partition": partition,
                "path": path, "row_count": len(result), "symbol_count": int(result[keys[-1]].nunique()) if keys else 0,
                "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
                "source_batches": [str(path) for _, path in raw_paths]}
