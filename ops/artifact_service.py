"""Read-only artifact details, summaries, previews and lineage."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pandas as pd

from StockInvestmentTool.ops.task_center import TaskCenter


class ArtifactService:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.center = TaskCenter(self.db_path, self.db_path)

    def detail(self, artifact_id: str) -> dict | None:
        rows = self.center.artifacts(artifact_id=artifact_id)
        if not rows:
            return None
        item = rows[0]
        path = Path(item["file_path"])
        item["exists"] = path.exists()
        if path.exists():
            item["actual_checksum"] = hashlib.sha256(path.read_bytes()).hexdigest()
            item["checksum_matches"] = item.get("checksum") == item["actual_checksum"]
        return item

    def summary(self, artifact_id: str) -> dict:
        detail = self.detail(artifact_id)
        if not detail or not detail["exists"]:
            raise FileNotFoundError(artifact_id)
        path = Path(detail["file_path"])
        if path.suffix == ".parquet":
            frame = pd.read_parquet(path)
        elif path.suffix == ".csv":
            frame = pd.read_csv(path)
        else:
            raise ValueError("仅支持 Parquet/CSV 摘要")
        missing = {column: int(frame[column].isna().sum()) for column in frame.columns}
        duplicates = int(frame.duplicated([c for c in ("date", "code") if c in frame.columns]).sum()) if any(c in frame.columns for c in ("date", "code")) else 0
        return {"artifact": detail, "columns": [{"name": c, "dtype": str(frame[c].dtype)} for c in frame.columns],
                "row_count": len(frame), "missing": missing, "duplicate_primary_keys": duplicates}

    def preview(self, artifact_id: str, limit=50, offset=0):
        return self.center.preview_artifact(artifact_id, limit=limit, offset=offset)

    def lineage(self, artifact_id: str):
        return self.center.lineage(artifact_id)
