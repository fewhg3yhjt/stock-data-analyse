"""Serial Tencent quote snapshot capture."""
from __future__ import annotations

import time
import os
from datetime import datetime

import pandas as pd
import re

from StockInvestmentTool.screener.sources import _parse_tencent_line
from StockInvestmentTool.warehouse.source_capture import capture_frames


def normalize_quotes(rows, *, trade_date: str, captured_at: str | None = None) -> pd.DataFrame:
    result = []
    for row in rows:
        item = dict(row)
        result.append({"trade_date": trade_date, "code": item.get("code"), "name": item.get("name"),
                       "price": item.get("price"), "total_mv": None if item.get("total_mcap") is None else item["total_mcap"] * 1e8,
                       "circ_mv": None if item.get("float_mcap") is None else item["float_mcap"] * 1e8,
                       "pe_ttm_source": item.get("pe_ttm"), "pb_source": item.get("pb"),
                       "turnover": item.get("turnover"), "volume_ratio": item.get("vol_ratio"),
                       "price_source": "tencent_quotes", "captured_at": captured_at or datetime.now().isoformat(timespec="seconds"),
                       "is_stale": bool(item.get("is_stale", False))})
    return pd.DataFrame(result)


def collect(warehouse, *, symbols: list[str], start_date: str, end_date: str,
            timeout: float = 15, deadline: float | None = None, batch: int = 60,
            fetcher=None, job_run_id: int | None = None,
            query_interval: float | None = None) -> dict:
    if not start_date or not end_date:
        raise ValueError("valuation_snapshot requires explicit start_date and end_date")
    if batch > 60:
        raise ValueError("Tencent batch must be <= 60")
    import requests
    fetcher = fetcher or (lambda url, timeout: requests.get(url, timeout=timeout).text)
    interval = max(0.0, float(query_interval if query_interval is not None
                              else os.getenv("TENCENT_QUOTE_QUERY_INTERVAL", "0.5")))
    deadline_at = time.monotonic() + deadline if deadline is not None else None
    rows = []
    failed = []
    request_count = 0
    for i in range(0, len(symbols), batch):
        if deadline_at is not None and time.monotonic() >= deadline_at:
            failed.extend(symbols[i:]); break
        chunk = symbols[i:i + batch]
        try:
            if request_count:
                time.sleep(interval)
            text = fetcher("https://qt.gtimg.cn/q=" + ",".join(chunk), timeout)
            request_count += 1
            rows.extend(parsed for line in text.splitlines() if (parsed := _parse_tencent_line(line)))
        except Exception as exc:  # noqa: BLE001
            failed.extend(chunk)
    trade_date = end_date
    frame = normalize_quotes(rows, trade_date=trade_date)
    if frame.empty:
        raise RuntimeError("valuation_snapshot capture returned no rows")
    raw = capture_frames(warehouse, dataset_name="valuation_snapshot", source_name="tencent_quotes", frames=[frame],
                         run_date=trade_date, trade_date_start=start_date, trade_date_end=trade_date,
                         expected_symbols=len(symbols), success_symbols=frame["code"].nunique(), failed_symbols=len(failed),
                         failure_details=[str(x) for x in failed], job_run_id=job_run_id,
                         schema_version="valuation_snapshot.v1")
    return {"rows": len(frame), "source_batch_id": raw["batch_id"], "failed": failed}


def build_candidate(warehouse, frame: pd.DataFrame, partition: str) -> dict:
    import hashlib
    path = warehouse.base_dir / "candidates" / "valuation_snapshot" / partition / f"valuation_snapshot_{partition}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return {"version_id": f"valuation_snapshot_{partition}", "dataset_name": "valuation_snapshot", "partition": partition,
            "path": path, "row_count": len(frame), "symbol_count": frame["code"].nunique(),
            "checksum": hashlib.sha256(path.read_bytes()).hexdigest()}
