import pandas as pd

from warehouse.legacy_adapters import adapt_fundamentals, adapt_legacy_file, adapt_stock_daily


def test_stock_daily_adapter_adds_pre_close_without_changing_source():
    source = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
        "code": ["sh.600000", "sh.600000"], "open": [10, 11], "high": [11, 12],
        "low": [9, 10], "close": [10.5, 11.5], "volume": [100, 110],
        "amount": [1000, 1200], "turn": [1, 1.1], "tradestatus": [1, 1],
    })
    result = adapt_stock_daily(source)
    assert "pre_close" in result
    assert pd.isna(result.iloc[0]["pre_close"])
    assert result.iloc[1]["pre_close"] == 10.5
    assert source["code"].iloc[0] == "sh.600000"


def test_fundamentals_adapter_maps_legacy_fields_and_filename_code():
    source = pd.DataFrame({
        "stat_date": ["2026-03-31"], "roe": [8.0], "gross_margin": [20.0],
        "asset_liability_ratio": [55.0],
    })
    result = adapt_fundamentals(source, code="sh600000")
    assert list(result.columns) == ["code", "stat_date", "roe", "gross_margin", "debt_ratio"]
    assert result.iloc[0]["code"] == "sh600000"
    assert result.iloc[0]["debt_ratio"] == 55.0


def test_valuation_adapter_normalizes_legacy_frame(tmp_path):
    source = tmp_path / "2026-01.parquet"
    pd.DataFrame({"date": ["2026-01-02"], "code": ["sh.600000"], "peTTM": [8], "pbMRQ": [1.2]}).to_parquet(source, index=False)
    result = adapt_legacy_file("valuation_daily", source)
    assert list(result.columns) == ["date", "code", "peTTM", "pbMRQ"]
    assert result.iloc[0]["code"] == "sh600000"
