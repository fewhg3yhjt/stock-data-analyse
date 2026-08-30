"""Adapters for adopting legacy warehouse files without mutating sources."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


STOCK_DAILY_COLUMNS = [
    "date", "code", "open", "high", "low", "close", "pre_close",
    "volume", "amount", "turn", "tradestatus",
]
FUNDAMENTAL_COLUMNS = [
    "code", "stat_date", "roe", "gross_margin", "debt_ratio",
]


def adapt_stock_daily(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize a legacy daily frame to ``stock_daily.v1`` columns."""
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["code"] = out["code"].astype(str).str.lower().str.replace(".", "", regex=False)
    out = out.sort_values(["code", "date"]).reset_index(drop=True)
    if "pre_close" not in out:
        out["pre_close"] = out.groupby("code")["close"].shift(1)
    for column in STOCK_DAILY_COLUMNS:
        if column not in out:
            out[column] = pd.NA
    return out[STOCK_DAILY_COLUMNS].sort_values(["date", "code"]).reset_index(drop=True)


def adapt_fundamentals(frame: pd.DataFrame, code: str | None = None) -> pd.DataFrame:
    """Normalize legacy financial files to ``fundamentals.v1`` columns."""
    out = frame.copy()
    if "code" not in out and code:
        out["code"] = code
    if "debt_ratio" not in out and "asset_liability_ratio" in out:
        out["debt_ratio"] = out["asset_liability_ratio"]
    out["code"] = out["code"].astype(str).str.lower().str.replace(".", "", regex=False)
    out["stat_date"] = pd.to_datetime(out["stat_date"], errors="coerce")
    for column in FUNDAMENTAL_COLUMNS:
        if column not in out:
            out[column] = pd.NA
    out = out[FUNDAMENTAL_COLUMNS].drop_duplicates(["code", "stat_date"])
    return out.sort_values(["code", "stat_date"]).reset_index(drop=True)


def adapt_legacy_file(dataset: str, source: Path) -> pd.DataFrame:
    """Read and adapt one legacy file; never writes to the source."""
    frame = pd.read_parquet(source)
    if dataset == "stock_daily":
        return adapt_stock_daily(frame)
    if dataset == "fundamentals":
        return adapt_fundamentals(frame, code=source.stem)
    if dataset == "valuation_daily":
        out = frame.copy()
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out["code"] = out["code"].astype(str).str.lower().str.replace(".", "", regex=False)
        for column in ("pe_ttm", "pb_mrq"):
            source_column = {"pe_ttm": "peTTM", "pb_mrq": "pbMRQ"}[column]
            if column not in out and source_column in out:
                out[column] = out[source_column]
            out[column] = pd.to_numeric(out[column], errors="coerce")
        return out[["date", "code", "pe_ttm", "pb_mrq"]].drop_duplicates(["date", "code"]).sort_values(["date", "code"]).reset_index(drop=True)
    raise ValueError(f"不支持的历史适配数据集: {dataset}")
