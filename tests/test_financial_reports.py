import pandas as pd
import pytest
import sqlite3

from StockInvestmentTool.warehouse.financial_reports import collect, financial_reports_quality
from StockInvestmentTool.warehouse.source_batches import SourceBatchStore
from StockInvestmentTool.warehouse.storage import Warehouse


HTML = """<table><tr><th>项目</th><th>2024-12-31</th></tr>
<tr><td>营业收入</td><td>10</td></tr>
<tr><td>归属于母公司股东的净利润</td><td>2</td></tr>
<tr><td>归属于母公司所有者权益合计</td><td>8</td></tr></table>"""


def test_financial_checkpoint_resume_merges_historical_raw(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        if "/vFD_ProfitStatement/" in url:
            return HTML
        raise RuntimeError("temporary failure")

    first = collect(warehouse, symbols=["sh600000"], start_date="2024-01-01", end_date="2024-12-31",
                    fetcher=fetch, max_retries=0, query_interval=0, sleep=lambda _: None)
    assert first["pending_count"] == 0
    batch = SourceBatchStore(warehouse.meta_db_path).get(first["source_batch_id"])
    assert batch["status"] == "partial_success"

    def resume_fetch(url, timeout):
        calls.append(url)
        return HTML

    second = collect(warehouse, symbols=["sh600000"], start_date="2024-01-01", end_date="2024-12-31",
                     checkpoint_batch_id=first["source_batch_id"], fetcher=resume_fetch,
                     max_retries=0, query_interval=0, sleep=lambda _: None)
    saved = pd.read_parquet(SourceBatchStore(warehouse.meta_db_path).get(second["source_batch_id"])["raw_path"])
    assert set(saved["statement_type"]) == {"profit", "balance"}
    assert len(saved.drop_duplicates(["report_date", "code", "statement_type"])) == len(saved)


def test_financial_checkpoint_success_items_are_skipped(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        return HTML

    first = collect(warehouse, symbols=["sh600000"], start_date="2024-01-01", end_date="2024-12-31",
                    fetcher=fetch, max_retries=0, query_interval=0, sleep=lambda _: None)
    count = len(calls)
    collect(warehouse, symbols=["sh600000"], start_date="2024-01-01", end_date="2024-12-31",
            checkpoint_batch_id=first["source_batch_id"], fetcher=fetch,
            max_retries=0, query_interval=0, sleep=lambda _: None)
    assert len(calls) == count


def test_financial_deadline_marks_remaining_items_timeout(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    with pytest.raises(RuntimeError, match="no rows"):
        collect(warehouse, symbols=["sh600000"], start_date="2024-01-01", end_date="2024-12-31",
                deadline=0, fetcher=lambda *_: HTML, max_retries=0, query_interval=0,
                sleep=lambda _: None)
    with sqlite3.connect(warehouse.meta_db_path) as conn:
        batch_id = conn.execute("SELECT batch_id FROM source_batches ORDER BY started_at DESC LIMIT 1").fetchone()[0]
    items = SourceBatchStore(warehouse.meta_db_path).list_items(batch_id)
    assert all(item["status"] == "timeout" for item in items)


def test_financial_consecutive_failures_pause(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    calls = 0

    def fetch(url, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return HTML
        raise RuntimeError("down")

    result = collect(warehouse, symbols=["sh600000", "sh600001"], start_date="2024-01-01", end_date="2024-12-31",
                     failure_threshold=1, fetcher=fetch,
                     max_retries=0, query_interval=0, sleep=lambda _: None)
    assert result["paused"] is True
    assert SourceBatchStore(warehouse.meta_db_path).get(result["source_batch_id"])["status"] == "paused"


def test_financial_quality_statement_coverage_and_core_rules():
    rows = []
    for code in ["sh600000", "sh600001", "sh600002", "sh600003", "sh600004"]:
        rows.extend([
            {"report_date": "2024-12-31", "code": code, "statement_type": "profit",
             "revenue": 1.0, "net_profit_parent": 1.0, "parent_equity": None},
            {"report_date": "2024-12-31", "code": code, "statement_type": "balance",
             "revenue": None, "net_profit_parent": None, "parent_equity": 1.0},
        ])
    frame = pd.DataFrame(rows)
    report = financial_reports_quality(frame, expected_symbols=["a", "b", "c", "d", "e"])
    assert report["status"] == "PASS"
    assert report["checks"]["statement_coverage"]["profit"]["coverage"] == 1.0
    assert report["checks"]["empty_financial_core_reasons"] == []
