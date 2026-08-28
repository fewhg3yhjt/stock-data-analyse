"""Config-driven quality checks for stock_daily candidates."""

from __future__ import annotations

import pandas as pd

from StockInvestmentTool.warehouse.dataset_config import load_dataset_config


def check_stock_daily(path, expected_symbols: int | None = None,
                      expected_trade_date: str | None = None,
                      config_path=None, source_conflicts: list[dict] | None = None) -> dict:
    frame = pd.read_parquet(path)
    config = load_dataset_config("stock_daily", config_path)["quality"]
    required = {"date", "code"}
    missing_columns = sorted(required - set(frame.columns))
    if missing_columns:
        return {"status": "FAIL", "publish_allowed": False,
                "checks": {"missing_columns": missing_columns}}

    duplicate = int(frame.duplicated(["date", "code"]).sum())
    numeric = {name: pd.to_numeric(frame[name], errors="coerce") for name in frame.columns}
    ohlc = {}
    for name in ("open", "high", "low", "close"):
        if name in numeric:
            ohlc[f"{name}_missing"] = int(numeric[name].isna().sum())
    invalid_mask = pd.Series(False, index=frame.index)
    if {"high", "open"}.issubset(numeric):
        invalid_mask |= numeric["high"].notna() & numeric["open"].notna() & (numeric["high"] < numeric["open"])
    if {"high", "close"}.issubset(numeric):
        invalid_mask |= numeric["high"].notna() & numeric["close"].notna() & (numeric["high"] < numeric["close"])
    if {"high", "low"}.issubset(numeric):
        invalid_mask |= numeric["high"].notna() & numeric["low"].notna() & (numeric["high"] < numeric["low"])
    if {"low", "open"}.issubset(numeric):
        invalid_mask |= numeric["low"].notna() & numeric["open"].notna() & (numeric["low"] > numeric["open"])
    if {"low", "close"}.issubset(numeric):
        invalid_mask |= numeric["low"].notna() & numeric["close"].notna() & (numeric["low"] > numeric["close"])
    for name in ("open", "high", "low", "close"):
        if name in numeric:
            invalid_mask |= numeric[name].notna() & (numeric[name] <= 0)
    negative = int(sum(int(numeric[name].lt(0).fillna(False).sum()) for name in ("volume", "amount") if name in numeric))
    symbols = int(frame["code"].astype(str).nunique())
    coverage = symbols / expected_symbols if expected_symbols else 1.0
    max_date = pd.to_datetime(frame["date"], errors="coerce").max()
    freshness = {"max_date": str(max_date)[:10] if pd.notna(max_date) else None}
    if expected_trade_date:
        freshness["expected_trade_date"] = expected_trade_date
        freshness["matches_expected"] = freshness["max_date"] == expected_trade_date
    if expected_trade_date and not freshness["matches_expected"]:
        freshness["status"] = "WARNING"
    else:
        freshness["status"] = "PASS"
    conflict_count = len(source_conflicts or [])

    checks = {
        "duplicate_primary_keys": duplicate,
        "ohlc": {**ohlc, "invalid_count": int(invalid_mask.sum())},
        "negative_volume_amount": negative,
        "symbol_count": symbols,
        "expected_symbols": expected_symbols,
        "coverage": coverage,
        "freshness": freshness,
        "source_conflict": {"count": conflict_count, "details": source_conflicts or []},
    }
    fail = duplicate > config.get("duplicates", {}).get("fail_if_gt", 0)
    fail |= int(invalid_mask.sum()) > config.get("ohlc", {}).get("fail_if_invalid_gt", 0)
    fail |= negative > 0
    fail |= coverage < config["coverage"]["warning_min"]
    conflict_config = config.get("source_conflict", {})
    conflict_ratio = conflict_count / max(1, len(frame))
    fail |= conflict_ratio > conflict_config.get("fail_ratio", 1.0)
    status = "FAIL" if fail else ("PASS" if coverage >= config["coverage"]["pass_min"] else "WARNING")
    if status == "PASS" and freshness["status"] == "WARNING":
        status = "WARNING"
    if status == "PASS" and conflict_ratio > conflict_config.get("warning_ratio", 1.0):
        status = "WARNING"
    publish_allowed = status == "PASS" or (status == "WARNING" and config["publish_warning"])
    return {"status": status, "publish_allowed": publish_allowed, "checks": checks}
