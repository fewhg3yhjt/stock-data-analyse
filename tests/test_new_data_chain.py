from __future__ import annotations

import pandas as pd

from StockInvestmentTool.warehouse.financial_reports import parse_sina_html
from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.valuation_build import build_valuation_daily, ttm_series
from StockInvestmentTool.warehouse.valuation_snapshot import normalize_quotes


def test_sina_legacy_html_parser_decodes_key_rows_and_converts_wan_yuan():
    html = """
    <table><tr><th>项目</th><th>2024-12-31</th><th>2023-12-31</th></tr>
    <tr><td>营业收入</td><td>12,345</td><td>10,000</td></tr>
    <tr><td>归属于母公司股东的净利润</td><td>1,234</td><td>900</td></tr></table>
    """
    result = parse_sina_html(html.encode("gb18030"), code="sh600000", statement_type="profit")
    assert result.loc[result.report_date == "2024-12-31", "revenue"].iloc[0] == 123_450_000
    assert result.loc[result.report_date == "2024-12-31", "net_profit_parent"].iloc[0] == 12_340_000


def test_sina_parser_selects_real_report_table_and_keeps_statement_fields_separate():
    html = """
    <table><tr><td>菜单</td><td>2026</td></tr></table>
    <table><tr><th>项目</th><th>2025-03-31</th><th>2024-12-31</th></tr>
    <tr><td>营业收入</td><td>100</td><td>400</td></tr>
    <tr><td>归属于母公司股东的净利润</td><td>10</td><td>40</td></tr></table>
    """
    result = parse_sina_html(html, code="sz000001", statement_type="profit")
    assert set(result.statement_type) == {"profit"}
    assert result.loc[result.report_date == "2025-03-31", "revenue"].iloc[0] == 1_000_000
    assert pd.isna(result.loc[result.report_date == "2025-03-31", "parent_equity"]).iloc[0]


def test_tencent_snapshot_converts_100_million_yuan_to_yuan():
    result = normalize_quotes([{"code": "sh600000", "total_mcap": 12.5, "float_mcap": 8.0, "is_stale": False}], trade_date="2026-09-04")
    assert result.iloc[0].total_mv == 1_250_000_000
    assert result.iloc[0].circ_mv == 800_000_000


def test_ttm_formula_uses_current_ytd_plus_prior_annual_minus_prior_ytd():
    frame = pd.DataFrame({"report_date": ["2024-03-31", "2024-12-31", "2025-03-31"], "revenue": [20., 100., 30.]})
    result = ttm_series(frame, "revenue")
    assert result[pd.Timestamp("2025-03-31")] == 110


def test_valuation_build_uses_snapshot_specific_financial_period_and_nulls_bad_denominators():
    from StockInvestmentTool.warehouse.valuation_build import build_valuation_daily, quality_report
    financial = pd.DataFrame([
        {"code": "sh600000", "statement_type": "profit", "report_date": "2024-12-31", "revenue": 100, "net_profit_parent": 20},
        {"code": "sh600000", "statement_type": "balance", "report_date": "2024-12-31", "parent_equity": 50},
        {"code": "sz000001", "statement_type": "profit", "report_date": "2024-12-31", "revenue": 0, "net_profit_parent": -1},
        {"code": "sz000001", "statement_type": "balance", "report_date": "2024-12-31", "parent_equity": 0},
    ])
    snapshots = pd.DataFrame([{"trade_date": "2025-01-02", "code": "sh600000", "price": 10, "total_mv": 1000, "circ_mv": 800},
                              {"trade_date": "2025-01-02", "code": "sz000001", "price": 10, "total_mv": 1000, "circ_mv": 800}])
    result = build_valuation_daily(financial, snapshots)
    assert result.loc[result.code == "sz000001", "pe_ttm_calc"].isna().all()
    assert quality_report(result, expected_symbols=2)["checks"]["duplicate_keys"] is True


def test_ttm_alignment_is_isolated_per_stock():
    from StockInvestmentTool.warehouse.valuation_build import build_valuation_daily
    financial = pd.DataFrame([
        {"code": "sh600000", "statement_type": "profit", "report_date": "2024-12-31", "revenue": 100, "net_profit_parent": 20},
        {"code": "sh600000", "statement_type": "balance", "report_date": "2024-12-31", "parent_equity": 50},
        {"code": "sz000001", "statement_type": "profit", "report_date": "2024-12-31", "revenue": 200, "net_profit_parent": 40},
        {"code": "sz000001", "statement_type": "balance", "report_date": "2024-12-31", "parent_equity": 100},
    ])
    snapshots = pd.DataFrame([
        {"trade_date": "2025-01-02", "code": "sh600000", "price": 10, "total_mv": 1000, "circ_mv": 800},
        {"trade_date": "2025-01-02", "code": "sz000001", "price": 10, "total_mv": 2000, "circ_mv": 800},
    ])
    result = build_valuation_daily(financial, snapshots).set_index("code")
    assert result.loc["sh600000", "revenue_ttm"] == 100
    assert result.loc["sz000001", "revenue_ttm"] == 200


def test_capture_frames_creates_raw_batch(tmp_path):
    from StockInvestmentTool.warehouse.storage import Warehouse
    warehouse = Warehouse(base_dir=tmp_path / "warehouse", meta_db_path=tmp_path / "management.db")
    captured = capture_frames(warehouse, dataset_name="valuation_snapshot", source_name="tencent_quotes",
                              frames=[pd.DataFrame({"trade_date": ["2026-09-04"], "code": ["sh600000"]})],
                              run_date="2026-09-04", trade_date_start="2026-09-04", trade_date_end="2026-09-04",
                              expected_symbols=1, success_symbols=1, schema_version="valuation_snapshot.v1")
    assert captured["batch_id"]
    assert captured["raw"]["row_count"] == 1


def test_source_quality_does_not_require_derived_ttm_fields(tmp_path):
    from StockInvestmentTool.ops.task_execution import _new_quality
    from StockInvestmentTool.warehouse.pipeline_state import PipelineState
    from StockInvestmentTool.warehouse.storage import Warehouse

    warehouse = Warehouse(base_dir=tmp_path / "warehouse", meta_db_path=tmp_path / "management.db")
    path = tmp_path / "snapshot.parquet"
    snapshot = pd.DataFrame({
        "trade_date": ["2026-09-04"], "code": ["sh600000"], "price": [9.0],
        "total_mv": [1e10], "circ_mv": [8e9],
    })
    snapshot.to_parquet(path, index=False)
    build = {"version_id": "valuation_snapshot_test", "partition": "2026-09",
             "path": path, "row_count": 1, "symbol_count": 1,
             "checksum": __import__("hashlib").sha256(path.read_bytes()).hexdigest()}
    version = PipelineState(warehouse.meta_db_path).create_version(
        build, source_batches=["fixture"], dataset_name="valuation_snapshot",
        schema_version="valuation_snapshot.v1")
    result = _new_quality(warehouse, {"input_versions": {"2026-09": version}}, "valuation_snapshot")
    assert result["status"] == "PASS"
