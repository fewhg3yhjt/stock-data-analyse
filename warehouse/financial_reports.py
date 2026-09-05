"""Read-only Sina legacy HTML financial report capture and normalization."""

from __future__ import annotations

import re
import time
import os
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import quote
from typing import Callable

import pandas as pd

from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.source_batches import SourceBatchStore

SINA_URLS = {
    "profit": "https://money.finance.sina.com.cn/corp/go.php/vFD_ProfitStatement/stockid/{code}/ctrl/{year}/displaytype/4.phtml",
    "balance": "https://money.finance.sina.com.cn/corp/go.php/vFD_BalanceSheet/stockid/{code}/ctrl/{year}/displaytype/4.phtml",
    "cash": "https://money.finance.sina.com.cn/corp/go.php/vFD_CashFlow/stockid/{code}/ctrl/{year}/displaytype/4.phtml",
}
VALUATION_STATEMENTS = ("profit", "balance")
KEY_ROWS = {
    "profit": {"revenue": ("营业收入", "营业总收入", "一、营业收入"), "net_profit_parent": ("归属于母公司股东的净利润", "归属于母公司所有者的净利润", "归属于母公司的净利润")},
    "balance": {"parent_equity": ("归属于母公司所有者权益合计", "归属于母公司股东权益合计", "归属于母公司股东的权益")},
}

ALL_FIELDS = ["report_date", "code", "statement_type", "revenue", "net_profit_parent", "parent_equity",
              "financial_publish_date", "source", "source_url", "unit", "captured_at"]


def _clean_number(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).replace(",", "").replace("，", "").strip()
    if not text or text in {"-", "--", "不适用"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def _date(value):
    parsed = pd.to_datetime(str(value), errors="coerce")
    return None if pd.isna(parsed) else parsed.strftime("%Y-%m-%d")


def parse_sina_html(html: str | bytes, *, code: str, statement_type: str,
                    source_url: str = "", captured_at: str | None = None) -> pd.DataFrame:
    """Parse Sina's old table layout without making any network request."""
    if isinstance(html, bytes):
        html = html.decode("gb18030", errors="replace")
    class _TableParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tables, self.rows, self.row, self.cell, self.in_table = [], [], [], None, False
        def handle_starttag(self, tag, attrs):
            if tag == "table": self.in_table, self.rows = True, []
            elif self.in_table and tag == "tr": self.row = []
            elif self.in_table and tag in {"td", "th"}: self.cell = []
        def handle_data(self, data):
            if self.in_table and self.cell is not None: self.cell.append(data)
        def handle_endtag(self, tag):
            if tag in {"td", "th"} and self.cell is not None:
                self.row.append(" ".join("".join(self.cell).split())); self.cell = None
            elif tag == "tr" and self.in_table and self.row:
                self.rows.append(self.row); self.row = []
            elif tag == "table" and self.in_table:
                if self.rows: self.tables.append(self.rows)
                self.in_table, self.rows = False, []
        def close(self):
            super().close()
            if self.in_table and self.rows: self.tables.append(self.rows)
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    tables = parser.tables
    if not tables:
        return pd.DataFrame()
    wanted = KEY_ROWS.get(statement_type, {})
    rows = []
    for raw_table in tables:
        if not raw_table or len(raw_table) < 2:
            continue
        width = max(map(len, raw_table))
        table = pd.DataFrame([row + [""] * (width - len(row)) for row in raw_table])
        # Real Sina pages have unrelated navigation tables before the report table.
        date_row = next((row for row in raw_table[:4] if sum(_date(x) is not None for x in row) >= 1), None)
        if date_row is None:
            continue
        dates = [_date(x) for x in date_row]
        date_positions = [i for i, value in enumerate(dates) if value]
        if not date_positions:
            continue
        for key, labels in wanted.items():
            hit = next((row for row in raw_table if str(row[0]).strip() in labels), None)
            if hit is None:
                continue
            for position in date_positions:
                row = {"report_date": dates[position], "code": code, "statement_type": statement_type,
                       key: _clean_number(hit[position] if position < len(hit) else None),
                       "financial_publish_date": None, "source": "sina_financial_html",
                       "source_url": source_url, "unit": "元",
                       "captured_at": captured_at or datetime.now().isoformat(timespec="seconds")}
                rows.append(row)
    if not rows:
        return pd.DataFrame()
    result = pd.DataFrame(rows)
    result = result.groupby(["report_date", "code", "statement_type"], as_index=False).first()
    for col in ALL_FIELDS:
        if col not in result: result[col] = pd.NA
    for col in ("revenue", "net_profit_parent", "parent_equity"):
        if col in result:
            result[col] = result[col] * 10000  # Sina reports financial statements in 万元.
    return result[ALL_FIELDS]


def normalize_reports(frame: pd.DataFrame) -> pd.DataFrame:
    required = ALL_FIELDS
    result = frame.copy()
    for col in required:
        if col not in result:
            result[col] = pd.NA
    result["report_date"] = pd.to_datetime(result["report_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    result["code"] = result["code"].astype(str).str.lower().str.replace(".", "", regex=False)
    for column in ("revenue", "net_profit_parent", "parent_equity"):
        result[column] = pd.to_numeric(result[column], errors="coerce").astype("float64")
    for column in ("financial_publish_date", "source", "source_url", "unit", "captured_at"):
        result[column] = result[column].astype("string")
    return result[required].dropna(subset=["report_date", "code", "statement_type"])


def collect(warehouse, *, symbols: list[str], start_date: str, end_date: str,
            timeout: float = 15, deadline: float | None = None, run_date: str | None = None,
            fetcher: Callable | None = None, job_run_id: int | None = None,
            query_interval: float | None = None, checkpoint_batch_id: str | None = None,
            failure_threshold: int = 20, sleep: Callable[[float], None] | None = None,
            max_retries: int = 2, symbol_limit: int | None = None,
            batch_size: int | None = None, max_symbols_per_run: int | None = None) -> dict:
    if not start_date or not end_date:
        raise ValueError("financial_reports requires explicit start_date and end_date")
    started = time.monotonic()
    deadline_at = started + deadline if deadline is not None else None
    fetcher = fetcher or _request
    sleep = sleep or time.sleep
    interval = max(0.0, float(query_interval if query_interval is not None
                              else os.getenv("SINA_FINANCIAL_QUERY_INTERVAL", "1.0")))
    limits = [value for value in (symbol_limit, batch_size, max_symbols_per_run) if value is not None]
    if any(int(value) <= 0 for value in limits):
        raise ValueError("financial_reports batch size must be positive")
    # Keep the three names as compatible aliases, while making an explicitly
    # supplied first name take precedence.
    symbol_batch_size = int(limits[0]) if limits else None
    request_count = 0
    frames, failed = [], []
    store = SourceBatchStore(warehouse.meta_db_path)
    years = range(pd.Timestamp(start_date).year, pd.Timestamp(end_date).year + 1)
    batch_id = checkpoint_batch_id
    if batch_id is None:
        batch_id = store.start(dataset_name="financial_reports", source_name="sina_financial_html",
                               run_date=run_date or end_date, trade_date_start=start_date,
                               trade_date_end=end_date, expected_symbols=len(symbols),
                               universe_id="financial_reports", request_context={
                                    "start_date": start_date, "end_date": end_date,
                                    "query_interval": interval, "failure_threshold": failure_threshold,
                                    "symbol_batch_size": symbol_batch_size},
                                job_run_id=job_run_id, schema_version="financial_reports.v1")
    # Items are created for the complete universe before selecting a run batch.
    # This is what makes a checkpoint durable even when the first invocation is
    # intentionally limited to a small number of symbols.
    known_items = {x["item_key"]: x for x in store.list_items(batch_id)}
    for code in sorted(set(symbols), key=str):
        for year in years:
            for statement_type in VALUATION_STATEMENTS:
                key = f"{code}:{year}:{statement_type}"
                if key not in known_items:
                    store.upsert_item(batch_id, item_key=key, symbol=code, period=str(year),
                                      statement_type=statement_type)
                    known_items[key] = {"item_key": key, "symbol": code}
    retryable_items = store.list_retryable(batch_id)
    retryable_symbols = sorted({item["symbol"] for item in retryable_items}, key=str)
    selected_symbols = (retryable_symbols[:symbol_batch_size]
                        if symbol_batch_size is not None else retryable_symbols)
    selected_set = set(selected_symbols)
    items = {item["item_key"]: item for item in retryable_items
             if item["symbol"] in selected_set}
    paused = False
    timed_out = False
    consecutive_failures = 0
    for item in items.values():
        code, year, statement_type = item["symbol"], int(item["period"]), item["statement_type"]
        key = item["item_key"]
        template = SINA_URLS[statement_type]
        if deadline_at is not None and time.monotonic() >= deadline_at:
            timed_out = True
            # This item was not started. Leave it pending for the next run;
            # in particular, do not turn an unprocessed batch into failures.
            break
        digits = re.sub(r"^[a-z]+", "", str(code).lower().replace(".", ""))
        url = template.format(code=quote(digits), year=year)
        success = False
        for attempt in range(max_retries + 1):
            try:
                store.update_item(batch_id, key, status="running", attempt_count=attempt + 1, last_error=None)
                if request_count:
                    sleep(interval)
                frame = parse_sina_html(fetcher(url, timeout), code=code, statement_type=statement_type, source_url=url)
                request_count += 1
                frame = frame[(frame["report_date"] >= start_date) & (frame["report_date"] <= end_date)] if not frame.empty else frame
                if frame.empty:
                    raise ValueError("empty response")
                frames.append(frame)
                store.update_item(batch_id, key, status="success", attempt_count=attempt + 1)
                consecutive_failures = 0
                success = True
                break
            except Exception as exc:  # noqa: BLE001
                wait = (5, 15)[min(attempt, 1)]
                final_item_status = "empty" if "empty response" in str(exc) and attempt == max_retries else (
                    "failed" if attempt == max_retries else "retrying")
                store.update_item(batch_id, key, status=final_item_status,
                                  attempt_count=attempt + 1, last_error=str(exc))
                if attempt < max_retries:
                    sleep(wait)
                else:
                    failed.append(f"{key}:{exc}")
                    consecutive_failures += 1
        if not success and consecutive_failures >= failure_threshold:
            paused = True
            break
    if deadline_at is not None and time.monotonic() >= deadline_at:
        timed_out = True
    success_items = store.list_success(batch_id)
    success_codes = {item["symbol"] for item in success_items}
    pending_count = len(store.list_pending(batch_id))
    remaining_retryable = store.list_retryable(batch_id)
    final_status = "paused" if paused else ("timeout" if timed_out else (
        "partial_success" if remaining_retryable or pending_count else "success"))
    # A resumed checkpoint may contain only already-successful items. Reuse its
    # immutable raw capture so a retry does not turn a valid checkpoint into an
    # empty capture.
    batch = store.get(batch_id) or {}
    historical = pd.DataFrame()
    raw_path = batch.get("raw_path")
    if raw_path and os.path.exists(raw_path):
        historical = pd.read_parquet(raw_path)
    if not historical.empty:
        frames.insert(0, historical)
    if not frames:
        store.finish_from_items(batch_id, row_count=0, raw_path=None, checksum=None, file_size=None,
                                status="paused" if paused else ("timeout" if timed_out else "failed"),
                                failure_details=failed)
        raise RuntimeError("financial_reports capture returned no rows")
    frames = [normalize_reports(frame) for frame in frames]
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.drop_duplicates(["report_date", "code", "statement_type"], keep="last")
    frames = [merged]
    successful_codes = success_codes
    raw = capture_frames(warehouse, dataset_name="financial_reports", source_name="sina_financial_html", frames=frames,
                         trade_date_start=start_date, trade_date_end=end_date, expected_symbols=len(symbols),
                         success_symbols=len(successful_codes),
                         failed_symbols=len(failed), failure_details=failed, job_run_id=job_run_id,
                          schema_version="financial_reports.v1", batch_id=batch_id, status=final_status)
    stats = store.finish_from_items(batch_id, row_count=raw["raw"]["row_count"],
                                    raw_path=str(raw["raw"]["path"]), checksum=raw["raw"]["checksum"],
                                    file_size=raw["raw"]["file_size"], status=final_status,
                                    failure_details=failed)
    next_batch_symbols = sorted({item["symbol"] for item in remaining_retryable}, key=str)
    batch_complete = not remaining_retryable and not store.list_pending(batch_id)
    return {"rows": sum(len(x) for x in frames), "source_batch_id": raw["batch_id"], "failed": failed,
             "checkpoint": {"batch_id": batch_id, "pending_count": stats["pending_count"]},
             "pending_count": stats["pending_count"], "success_symbols": sorted(success_codes),
             "failed_items": failed, "paused": paused, "batch_complete": batch_complete,
             "next_batch_symbols": next_batch_symbols}


def financial_reports_quality(frame: pd.DataFrame, *, expected_symbols: int | None = None,
                              expected_statement_types: tuple[str, ...] = VALUATION_STATEMENTS) -> dict:
    """Full financial coverage gate: symbol and statement coverage, not just row shape."""
    required = {"report_date", "code", "statement_type"}
    missing = sorted(required - set(frame.columns))
    if missing:
        return {"status": "FAIL", "publish_allowed": False, "checks": {"missing_columns": missing}}
    symbols = set(frame["code"].dropna().astype(str))
    expected = len(expected_symbols) if isinstance(expected_symbols, (list, tuple, set)) else (expected_symbols or 0)
    checks = {"row_count": int(len(frame)), "symbol_count": len(symbols),
              "expected_symbols": expected, "coverage": len(symbols) / expected if expected else None,
              "statement_coverage": {}}
    for statement in expected_statement_types:
        count = frame.loc[frame["statement_type"].eq(statement), "code"].nunique()
        checks["statement_coverage"][statement] = {"success_symbols": int(count),
            "coverage": count / expected if expected else None}
    checks["empty_financial_core_reasons"] = []
    core_by_statement = {"profit": ("revenue", "net_profit_parent"),
                         "balance": ("parent_equity",)}
    for statement, columns in core_by_statement.items():
        subset = frame[frame["statement_type"].eq(statement)]
        present = [column for column in columns if column in subset]
        if present and subset[present].isna().all(axis=1).any():
            checks["empty_financial_core_reasons"].append(f"{statement}:core_fields_null")
    ratios = [checks["coverage"]] + [v["coverage"] for v in checks["statement_coverage"].values()]
    minimum = min((x for x in ratios if x is not None), default=0)
    status = "FAIL" if minimum < .95 or checks["row_count"] == 0 else ("WARNING" if minimum < .98 else "PASS")
    return {"status": status, "publish_allowed": status != "FAIL", "checks": checks}


def _request(url: str, timeout: float):
    import requests
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    response.encoding = "gb18030"
    return response.content


def build_candidate(warehouse, frame: pd.DataFrame, partition: str) -> dict:
    from StockInvestmentTool.warehouse.dataset_build import DatasetBuilder
    path = warehouse.base_dir / "candidates" / "financial_reports" / partition / f"financial_reports_{partition}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    normalize_reports(frame).to_parquet(path, index=False)
    return {"version_id": f"financial_reports_{partition}", "dataset_name": "financial_reports", "partition": partition,
            "path": path, "row_count": len(frame), "symbol_count": frame["code"].nunique(),
            "checksum": __import__("hashlib").sha256(path.read_bytes()).hexdigest()}
