import pandas as pd

from StockInvestmentTool.warehouse.industry import (
    IndustryCollector, normalize_industry_daily, normalize_membership, parse_baostock_industry,
)
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.quality import check_industry_daily, check_industry_membership, check_industry_features_daily
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
    assert raw.iloc[0]["industry_classification"] == "csrc"


def test_industry_features_quality_checks_grain_and_as_of(tmp_path):
    frame = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-04"]), "industry_code": ["I64"],
        "industry_name": ["互联网"], "industry_classification": ["csrc"],
        "member_count": [2], "valid_count": [2], "up_count": [1], "down_count": [1], "up_ratio": [.5],
        "return_1d": [.1], "return_3d": [.1], "return_5d": [.1], "return_10d": [.1], "return_20d": [.1],
        "amount": [10.], "amount_ma5": [10.], "amount_ma20": [10.], "amount_ratio": [1.],
        "rank_1d": [1], "rank_5d": [1], "rank_20d": [1], "leader_code": ["sh600000"],
        "leader_return": [.1], "leader_amount": [10.], "industry_score": [.8],
        "industry_state": ["strong"], "state_reason": ["ok"],
    })
    path = tmp_path / "features.parquet"; frame.to_parquet(path, index=False)
    assert check_industry_features_daily(path, expected_industries=1, expected_as_of="2026-09-05")["status"] == "PASS"
    bad = frame.assign(date=pd.Timestamp("2026-09-06")); bad.to_parquet(path, index=False)
    assert check_industry_features_daily(path, expected_as_of="2026-09-05")["status"] == "FAIL"


def _write(frame):
    import tempfile
    path = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False).name
    frame.to_parquet(path, index=False)
    return path
