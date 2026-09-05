"""Read-only Sina legacy HTML financial report capture and normalization."""

from __future__ import annotations

import re
import time
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import quote
from typing import Callable

import pandas as pd

from StockInvestmentTool.warehouse.source_capture import capture_frames

SINA_URLS = {
    "profit": "https://money.finance.sina.com.cn/corp/go.php/vFD_ProfitStatement/stockid/{code}/ctrl/{year}/displaytype/4.phtml",
    "balance": "https://money.finance.sina.com.cn/corp/go.php/vFD_BalanceSheet/stockid/{code}/ctrl/{year}/displaytype/4.phtml",
    "cash": "https://money.finance.sina.com.cn/corp/go.php/vFD_CashFlow/stockid/{code}/ctrl/{year}/displaytype/4.phtml",
}
KEY_ROWS = {
    "profit": {"revenue": ("营业收入", "营业总收入"), "net_profit_parent": ("归属于母公司股东的净利润", "归属于母公司所有者的净利润")},
    "balance": {"parent_equity": ("归属于母公司所有者权益合计", "归属于母公司股东权益合计")},
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
    return result[required].dropna(subset=["report_date", "code", "statement_type"])


def collect(warehouse, *, symbols: list[str], start_date: str, end_date: str,
            timeout: float = 15, deadline: float | None = None, run_date: str | None = None,
            fetcher: Callable | None = None, job_run_id: int | None = None) -> dict:
    if not start_date or not end_date:
        raise ValueError("financial_reports requires explicit start_date and end_date")
    started = time.monotonic()
    deadline_at = started + deadline if deadline is not None else None
    fetcher = fetcher or _request
    frames, failed = [], []
    for code in symbols:
        for statement_type, template in SINA_URLS.items():
            if deadline_at is not None and time.monotonic() >= deadline_at:
                failed.append(f"{code}:{statement_type}:deadline")
                continue
            digits = re.sub(r"^[a-z]+", "", str(code).lower().replace(".", ""))
            year = pd.Timestamp(end_date).year
            url = template.format(code=quote(digits), year=year)
            try:
                frame = parse_sina_html(fetcher(url, timeout), code=code, statement_type=statement_type, source_url=url)
                frame = frame[(frame["report_date"] >= start_date) & (frame["report_date"] <= end_date)] if not frame.empty else frame
                if frame.empty:
                    failed.append(f"{code}:{statement_type}:empty")
                else:
                    frames.append(frame)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{code}:{statement_type}:{exc}")
    if not frames:
        capture_frames(warehouse, dataset_name="financial_reports", source_name="sina_financial_html", frames=[],
                       trade_date_start=start_date, trade_date_end=end_date, expected_symbols=len(symbols),
                       failed_symbols=len(failed), failure_details=failed, job_run_id=job_run_id,
                       schema_version="financial_reports.v1")
        raise RuntimeError("financial_reports capture returned no rows")
    successful_codes = {frame["code"].iloc[0] for frame in frames if not frame.empty}
    raw = capture_frames(warehouse, dataset_name="financial_reports", source_name="sina_financial_html", frames=frames,
                         trade_date_start=start_date, trade_date_end=end_date, expected_symbols=len(symbols),
                         success_symbols=len(successful_codes),
                         failed_symbols=len(failed), failure_details=failed, job_run_id=job_run_id,
                         schema_version="financial_reports.v1")
    return {"rows": sum(len(x) for x in frames), "source_batch_id": raw["batch_id"], "failed": failed}


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
