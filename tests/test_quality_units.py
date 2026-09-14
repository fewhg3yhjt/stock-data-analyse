import pandas as pd

from StockInvestmentTool.warehouse.quality import check_stock_daily


def test_stock_daily_unit_exception_is_scoped_to_code_and_date(tmp_path):
    frame = pd.DataFrame({
        "date": pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03"]),
        "code": ["sz159582", "sz159582", "sz159582"],
        "open": [1, 1, 1], "high": [1, 1, 1], "low": [1, 1, 1], "close": [1, 1, 1],
        "volume": [100, 100, 100], "amount": [6, 6, 6],
    })
    path = tmp_path / "daily.parquet"
    frame.to_parquet(path, index=False)
    config = tmp_path / "dataset.yaml"
    config.write_text("""dataset:\n  name: stock_daily\n  display_name: daily\n  description: daily\n  grain: day\n  primary_keys: [date, code]\n  partition: {type: month, path: warehouse/daily/{partition}.parquet}\n  update_frequency: daily\n  schema_version: v1\nfields:\n  - {name: date, display_name: date, data_type: date, nullable: false, primary_key: true}\n  - {name: code, display_name: code, data_type: string, nullable: false, primary_key: true}\n  - {name: open, display_name: open, data_type: float, unit: yuan, nullable: true}\n  - {name: high, display_name: high, data_type: float, unit: yuan, nullable: true}\n  - {name: low, display_name: low, data_type: float, unit: yuan, nullable: true}\n  - {name: close, display_name: close, data_type: float, unit: yuan, nullable: false}\n  - {name: volume, display_name: volume, data_type: float, unit: share, nullable: false}\n  - {name: amount, display_name: amount, data_type: float, unit: yuan, nullable: false}\nsources: [{name: tencent, role: primary, priority: 1, field_mapping: {}, unit_conversions: {}}]\nconsumers: []\nquality:\n  publish_warning: true\n  unit_consistency:\n    implied_amount_volume_close_min: 0.2\n    implied_amount_volume_close_max: 5.0\n    exceptions:\n      - {name: scoped, codes: [sz159582], dates: [2026-07-01, 2026-07-02], max: 10.0, reason: test}\n  coverage: {pass_min: 0.0, warning_min: 0.0}\n  duplicates: {fail_if_gt: 0}\n  ohlc: {fail_if_invalid_gt: 0}\n  source_conflict: {warning_ratio: 1.0, fail_ratio: 1.0}\n""", encoding="utf-8")
    result = check_stock_daily(path, expected_symbols=1, config_path=config)
    assert result["status"] == "FAIL"
    assert result["checks"]["unit_consistency"]["abnormal_count"] == 1
    assert result["checks"]["unit_consistency"]["exceptions_applied"][0]["rows"] == 2
