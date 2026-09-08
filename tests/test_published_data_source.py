import pandas as pd


def test_default_data_source_does_not_online_fallback(monkeypatch):
    from StockInvestmentTool.datasource.base import get_default_datasource

    source = get_default_datasource()
    monkeypatch.setattr(source, "_load_published_daily", lambda *args, **kwargs: pd.DataFrame())
    assert source.fetch_kline("sh600000", "2026-09-01", "2026-09-07").empty


def test_default_source_is_not_explicit_fallback_chain():
    from StockInvestmentTool.datasource.base import FallbackDataSource, get_default_datasource

    assert not isinstance(get_default_datasource(), FallbackDataSource)
