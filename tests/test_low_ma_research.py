"""LowMA research strategy contract tests."""

import pandas as pd
import pytest

from StockInvestmentTool.research.low_ma import (
    LowMAConfig,
    _fill_price,
    build_low_ma_features,
    run_low_ma_experiment,
)


def make_frame(rows):
    dates = pd.bdate_range("2026-01-01", periods=len(rows))
    return pd.DataFrame(
        [{"date": date, "code": "sz000001", **row} for date, row in zip(dates, rows)]
    )


def test_features_do_not_use_current_day_for_prior_low_or_volume_average():
    frame = make_frame([
        {"open": 10, "high": 11, "low": 9, "close": 10, "volume": 100},
    ] * 5 + [
        {"open": 9, "high": 9.5, "low": 8, "close": 8.5, "volume": 1000},
    ] * 20)
    features = build_low_ma_features(frame)
    row = features.iloc[5]
    assert row["volume_avg5_prior"] == pytest.approx(100)
    assert pd.isna(row["prior20_low"])

    row = features.iloc[20]
    assert row["prior20_low"] == pytest.approx(8)
    assert not bool(row["new20_low"])


def test_limit_fill_uses_range_and_open_price_improvement():
    row = pd.Series({"open": 9.5, "high": 10.5, "low": 9.4})
    assert _fill_price(row, 10.0) == pytest.approx(9.5)
    assert _fill_price(pd.Series({"open": 9.5, "high": 9.8, "low": 9.4}), 10.0) == pytest.approx(9.5)
    assert _fill_price(pd.Series({"open": 10.5, "high": 10.8, "low": 10.2}), 10.0) is None


def test_structure_stop_requires_consecutive_closes_and_executes_next_open():
    rows = [{"open": 10, "high": 10.5, "low": 9.5, "close": 10, "volume": 100}] * 25
    rows += [
        {"open": 9.8, "high": 10.1, "low": 9.7, "close": 9.9, "volume": 100},
        {"open": 9.7, "high": 9.9, "low": 9.0, "close": 9.1, "volume": 100},
        {"open": 8.8, "high": 9.0, "low": 8.7, "close": 8.9, "volume": 100},
        {"open": 8.5, "high": 8.7, "low": 8.4, "close": 8.6, "volume": 100},
        {"open": 8.3, "high": 8.5, "low": 8.2, "close": 8.4, "volume": 100},
    ]
    frame = make_frame(rows)
    features = build_low_ma_features(frame)
    features.loc[25, "base_qualified"] = True
    features.loc[25, "lowma5"] = 9.8
    # Use the baseline path so the fixture tests the exit state machine rather
    # than depending on a second synthetic Path A volume pattern.
    import StockInvestmentTool.research.low_ma as low_ma
    original_builder = low_ma.build_low_ma_features
    low_ma.build_low_ma_features = lambda _: features
    try:
        result = run_low_ma_experiment(features, LowMAConfig(
            entry_path="baseline", position_limit=1.0,
            breakeven_activation=None, trend_activation=None,
        ))
    finally:
        low_ma.build_low_ma_features = original_builder
    exits = result["events"][result["events"]["event"] == "EXIT_SIGNAL"]
    assert not exits.empty
    trade = result["trades"].iloc[0]
    assert trade["exit_reason"] == "STRUCTURE_STOP"
    assert pd.Timestamp(trade["exit_date"]) > pd.Timestamp(trade["exit_signal_date"])


def test_invalid_config_is_rejected():
    with pytest.raises(ValueError):
        LowMAConfig(entry_path="unknown")


def test_path_thresholds_are_configurable():
    frame = make_frame([
        {"open": 9.7, "high": 10.5, "low": 9.5, "close": 9.7, "volume": 100},
    ] * 25 + [
        {"open": 9.7, "high": 10.5, "low": 9.5, "close": 9.7, "volume": 100},
    ])
    config = LowMAConfig(
        entry_path="path_a", close_location_low=0.2,
        close_location_high=0.9, sell_volume_ratio_low=0.5,
        sell_volume_ratio_high=2.0, breakeven_activation=None,
        trend_activation=None,
    )
    import StockInvestmentTool.research.low_ma as low_ma
    original_builder = low_ma.build_low_ma_features
    prepared = build_low_ma_features(frame)
    prepared.loc[24, "base_qualified"] = True
    prepared.loc[24, "close_location"] = 0.5
    prepared.loc[24, "sell_volume_ratio"] = 1.0
    prepared.loc[24, "path_a"] = True
    low_ma.build_low_ma_features = lambda _: prepared
    try:
        result = run_low_ma_experiment(frame, config)
    finally:
        low_ma.build_low_ma_features = original_builder
    assert not result["features"].empty
    assert bool(result["features"].iloc[24]["path_a"])


def test_dataset_runner_requires_explicit_dates(monkeypatch):
    from StockInvestmentTool.research.low_ma import run_low_ma_dataset
    with pytest.raises(ValueError):
        run_low_ma_dataset(object(), start_date="", end_date="2026-01-02")


def test_experiment_returns_kline_and_curve_contract():
    frame = make_frame([
        {"open": 10, "high": 11, "low": 9, "close": 10, "volume": 100},
    ] * 25 + [
        {"open": 9.5, "high": 10.5, "low": 9.4, "close": 10, "volume": 100},
    ])
    prepared = build_low_ma_features(frame)
    prepared.loc[24, "base_qualified"] = True
    prepared.loc[24, "lowma5"] = 10.0
    import StockInvestmentTool.research.low_ma as low_ma
    original_builder = low_ma.build_low_ma_features
    low_ma.build_low_ma_features = lambda _: prepared
    try:
        result = run_low_ma_experiment(frame, LowMAConfig(
            entry_path="baseline", breakeven_activation=None,
            trend_activation=None,
        ))
    finally:
        low_ma.build_low_ma_features = original_builder
    assert len(result["kline"]) == len(frame)
    assert len(result["curves"]) == len(frame)
