"""Prepare date-scoped stock_daily Raw repair directories."""

from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Iterable

import pandas as pd

from StockInvestmentTool.ops.trade_calendar import is_trade_day


def _codes(values: Iterable[str]) -> set[str]:
    return {
        str(value).strip().lower().replace(".", "")
        for value in values
        if str(value).strip()
    }


def _parquet_codes(paths: list[Path]) -> set[str]:
    found: set[str] = set()
    for path in paths:
        frame = pd.read_parquet(path, columns=["code"])
        found.update(_codes(frame["code"].tolist()))
    return found


def prepare_stock_daily_raw_range(
    base_dir: Path | str,
    start_date: str,
    end_date: str,
    reference_codes: Iterable[str],
    *,
    clean_empty_non_trading: bool = False,
) -> dict:
    """Initialize trading-date Raw directories and pending-code CSVs.

    This function is intentionally network-free. It creates only trading-day
    directories and computes missing codes from the formal and ``_tmp`` files
    already present for that date.
    """
    start = date.fromisoformat(str(start_date)[:10])
    end = date.fromisoformat(str(end_date)[:10])
    if end < start:
        raise ValueError("end_date 不能早于 start_date")
    reference = _codes(reference_codes)
    if not reference:
        raise ValueError("reference_codes 不能为空")

    root = Path(base_dir)
    results = []
    for timestamp in pd.date_range(start, end, freq="D"):
        day = timestamp.date()
        directory = root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        if not is_trade_day(day):
            if clean_empty_non_trading and directory.exists():
                parquet = list(directory.glob("*.parquet")) + list((directory / "_tmp").glob("*.parquet"))
                if not parquet and not list(directory.glob("*.tmp")):
                    for child in directory.iterdir():
                        if child.is_file():
                            child.unlink()
                    for child in list(directory.iterdir()):
                        if child.is_dir():
                            child.rmdir()
                    directory.rmdir()
            results.append({"date": day.isoformat(), "trading": False, "pending": 0})
            continue

        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / "_tmp"
        tmp.mkdir(exist_ok=True)
        paths = list(directory.glob("*.parquet")) + list(tmp.glob("*.parquet"))
        missing = sorted(reference - _parquet_codes(paths))
        pending_path = directory / "pending_codes.csv"
        frame = pd.DataFrame({"date": [day.isoformat()] * len(missing), "code": missing})
        fd, temp_name = tempfile.mkstemp(prefix=".pending_", suffix=".csv", dir=directory)
        os.close(fd)
        temp = Path(temp_name)
        try:
            frame.to_csv(temp, index=False)
            os.replace(temp, pending_path)
        finally:
            temp.unlink(missing_ok=True)
        results.append({"date": day.isoformat(), "trading": True,
                        "pending": len(missing), "path": str(pending_path)})
    return {"start_date": start.isoformat(), "end_date": end.isoformat(),
            "reference_symbols": len(reference), "dates": results}
