"""Read-only baseline audit for the stock_daily warehouse."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd


def _schema(df: pd.DataFrame) -> list[dict[str, str]]:
    return [{"name": name, "dtype": str(dtype)} for name, dtype in df.dtypes.items()]


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_partition(path: Path) -> dict[str, Any]:
    """Summarize one Parquet partition without modifying it."""
    df = pd.read_parquet(path)
    duplicate_count = 0
    if {"date", "code"}.issubset(df.columns):
        duplicate_count = int(df.duplicated(["date", "code"]).sum())
    dates = pd.to_datetime(df["date"], errors="coerce") if "date" in df.columns else pd.Series(dtype="datetime64[ns]")
    codes = df["code"].astype(str) if "code" in df.columns else pd.Series(dtype=str)
    return {
        "partition": path.stem,
        "path": str(path),
        "file_size": path.stat().st_size,
        "checksum": _checksum(path),
        "row_count": int(len(df)),
        "symbol_count": int(codes.nunique()),
        "min_date": dates.min().date().isoformat() if dates.notna().any() else None,
        "max_date": dates.max().date().isoformat() if dates.notna().any() else None,
        "duplicate_primary_keys": duplicate_count,
        "schema": _schema(df),
        "columns": list(df.columns),
    }


def audit_stock_daily(base_dir: Path, months: Optional[list[str]] = None) -> dict[str, Any]:
    """Create a deterministic, read-only audit report for daily partitions."""
    daily_dir = Path(base_dir) / "daily"
    selected = set(months or [])
    paths = sorted(daily_dir.glob("*.parquet"))
    if selected:
        paths = [path for path in paths if path.stem in selected]
    partitions = [audit_partition(path) for path in paths]
    return {
        "report_type": "stock_daily_baseline",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_dir": str(Path(base_dir)),
        "partition_count": len(partitions),
        "row_count": sum(item["row_count"] for item in partitions),
        "symbol_count": len({
            code
            for path in paths
            for code in pd.read_parquet(path)["code"].astype(str).unique()
        }) if paths and all("code" in pd.read_parquet(path, columns=["code"]).columns for path in paths) else None,
        "duplicate_primary_keys": sum(item["duplicate_primary_keys"] for item in partitions),
        "partitions": partitions,
    }


def write_report(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path
