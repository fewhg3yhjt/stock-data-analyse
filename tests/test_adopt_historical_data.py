def test_adoption_module_exposes_supported_datasets():
    from scripts.adopt_historical_data import DATASETS
    assert set(DATASETS) == {"stock_daily", "valuation_daily", "fundamentals", "indicators", "industry", "money_flow_daily"}
