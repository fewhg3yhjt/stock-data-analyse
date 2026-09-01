import pandas as pd

from StockInvestmentTool.warehouse.industry import (
    IndustryCollector, normalize_industry_daily, normalize_membership, parse_baostock_industry,
)
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.quality import check_industry_daily, check_industry_membership
from StockInvestmentTool.warehouse.storage import Warehouse


def test_membership_normalizer_keeps_raw_and_parses_baostock_value():
    assert parse_baostock_industry("I64互联网和相关服务") == ("I64", "互联网和相关服务")
    frame = normalize_membership(pd.DataFrame({"code": ["sh600000"], "industry": ["I64互联网和相关服务"]}), snapshot_date="2026-09-02")
    assert frame.iloc[0]["industry_code"] == "I64"
    assert frame.iloc[0]["raw_industry"] == "I64互联网和相关服务"
    assert check_industry_membership(_write(frame), expected_symbols=1)["status"] == "PASS"


def test_daily_normalizer_maps_ths_columns_and_quality_rejects_bad_ohlc(tmp_path):
    frame = normalize_industry_daily(pd.DataFrame({
        "日期": ["2026-09-01"], "开盘": [10], "最高": [11], "最低": [9],
        "收盘": [10.5], "成交量": [100], "成交额": [1000],
    }), industry_id="880001", industry_name="银行")
    path = tmp_path / "candidate.parquet"
    frame.to_parquet(path, index=False)
    assert check_industry_daily(path, expected_symbols=1)["status"] == "PASS"


def test_collector_is_serial_retrying_and_checkpointed(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    warehouse.metadata.register_dataset("industry_membership")
    calls = []
    attempts = {"sh600000": 0}

    def query(code):
        calls.append(code)
        attempts[code] += 1
        if attempts[code] == 1:
            raise RuntimeError("temporary")
        return "I64互联网和相关服务"

    collector = IndustryCollector(warehouse, baostock_query=query, interval=0, backoff=0,
                                  sleep=lambda _: None, checkpoint_dir=tmp_path / "checkpoints")
    result = collector.collect_membership(["sh600000"], snapshot_date="2026-09-02")
    assert result["success"] == 1
    assert calls == ["sh600000", "sh600000"]
    again = collector.collect_membership(["sh600000"], snapshot_date="2026-09-02")
    assert again["skipped"] == 1


def test_daily_normalizer_accepts_actual_akshare_fields():
    frame = normalize_industry_daily(pd.DataFrame({
        "日期": ["2026-09-01"], "开盘价": [10], "最高价": [11], "最低价": [9],
        "收盘价": [10.5], "成交量": [100], "成交额": [1000],
    }), industry_id="881121", industry_name="半导体")
    assert frame.iloc[0]["industry_id"] == "881121"
    assert frame.iloc[0]["open"] == 10
    assert frame.iloc[0]["close"] == 10.5


def test_membership_dict_keeps_source_metadata(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    calls = []

    def query(code):
        calls.append(code)
        return {"industry": "I64互联网和相关服务", "updateDate": "2026-08-31",
                "industryClassification": "证监会行业分类"}

    collector = IndustryCollector(warehouse, baostock_query=query, interval=0,
                                  sleep=lambda _: None, checkpoint_dir=tmp_path / "checkpoints")
    result = collector.collect_membership(["sh600000"], snapshot_date="2026-09-02")
    assert result["success"] == 1
    raw = pd.read_parquet(warehouse.raw.batch_dir("baostock", "industry_membership", "2026-09-02").glob("batch_*.parquet").__iter__().__next__())
    assert raw.iloc[0]["source_update_date"] == "2026-08-31"
    assert raw.iloc[0]["industry_classification"] == "证监会行业分类"


def _write(frame):
    import tempfile
    path = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False).name
    frame.to_parquet(path, index=False)
    return path
