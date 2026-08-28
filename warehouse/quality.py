"""V1 quality checks for a candidate stock_daily partition."""

from __future__ import annotations

import pandas as pd

from StockInvestmentTool.warehouse.dataset_config import load_dataset_config


def check_stock_daily(path, expected_symbols: int | None = None, config_path=None) -> dict:
    frame = pd.read_parquet(path)
    duplicate = int(frame.duplicated(["date", "code"]).sum())
    ohlc_invalid = int(((frame["high"] < frame["low"]) | (frame["close"] <= 0)).fillna(False).sum()) if {"high", "low", "close"}.issubset(frame) else 0
    negative = int(((frame["volume"] < 0) | (frame["amount"] < 0)).fillna(False).sum()) if {"volume", "amount"}.issubset(frame) else 0
    symbols = int(frame["code"].nunique())
    coverage = symbols / expected_symbols if expected_symbols else 1.0
    config = load_dataset_config("stock_daily", config_path)["quality"]
    status = "FAIL" if duplicate or ohlc_invalid or negative or coverage < config["coverage"]["warning_min"] else ("PASS" if coverage >= config["coverage"]["pass_min"] else "WARNING")
    return {"status": status, "publish_allowed": status != "FAIL" and (status == "PASS" or config["publish_warning"]),
            "checks": {"duplicate_primary_keys": duplicate, "ohlc_invalid": ohlc_invalid,
                        "negative_volume_amount": negative, "symbol_count": symbols, "coverage": coverage}}
