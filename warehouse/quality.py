"""Config-driven quality checks for stock_daily candidates."""

from __future__ import annotations

from pathlib import Path

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
    row_count = int(len(frame))
    if row_count == 0:
        return {"status": "FAIL", "publish_allowed": False,
                "checks": {"row_count": 0, "empty": True}}
    symbols = int(frame["code"].astype(str).nunique())
    if symbols == 0:
        return {"status": "FAIL", "publish_allowed": False,
                "checks": {"symbol_count": 0, "empty_symbols": True}}

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
    coverage = symbols / expected_symbols if expected_symbols else None
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
    fail |= coverage is not None and coverage < config["coverage"]["warning_min"]
    conflict_config = config.get("source_conflict", {})
    conflict_ratio = conflict_count / max(1, len(frame))
    fail |= conflict_ratio > conflict_config.get("fail_ratio", 1.0)
    status = "FAIL" if fail else (
        "PASS" if coverage is not None and coverage >= config["coverage"]["pass_min"]
        else "WARNING")
    if status == "PASS" and freshness["status"] == "WARNING":
        status = "WARNING"
    if status == "PASS" and conflict_ratio > conflict_config.get("warning_ratio", 1.0):
        status = "WARNING"
    publish_allowed = status == "PASS" or (status == "WARNING" and config["publish_warning"])
    return {"status": status, "publish_allowed": publish_allowed, "checks": checks}


def check_derived_output(path, dataset_name: str, *, expected_symbols: int | None = None,
                         expected_trade_date: str | None = None,
                         required_columns: list[str] | None = None,
                         core_non_null_columns: list[str] | None = None,
                         input_versions: dict | None = None) -> dict:
    """独立质量检查派生数据集（indicators 等）。

    与 stock_daily 的 check_stock_daily 分离，避免"输入版本自动 PASS"的自证。
    空数据保护：row_count=0、symbol_count=0、必填字段全空均 FAIL。

    Args:
        path: 派生 Parquet 文件路径。
        dataset_name: 数据集名（如 indicators）。
        expected_symbols: 期望覆盖标的数（来自上游 Universe/请求固化）。
        expected_trade_date: 期望日期（YYYY-MM-DD）。
        required_columns: 必填字段（缺失即 FAIL），默认 ["date","code"]。
        core_non_null_columns: 核心字段，非空率过低时 WARNING。
        input_versions: 输入数据集版本引用 {dataset: {partition: version}}。
    """
    path = Path(path)
    if not path.exists():
        return {"status": "FAIL", "publish_allowed": False,
                "checks": {"file_missing": str(path)}}
    frame = pd.read_parquet(path)
    required = required_columns or ["date", "code"]
    missing_columns = sorted(set(required) - set(frame.columns))
    if missing_columns:
        return {"status": "FAIL", "publish_allowed": False,
                "checks": {"missing_columns": missing_columns}}
    row_count = int(len(frame))
    if row_count == 0:
        return {"status": "FAIL", "publish_allowed": False,
                "checks": {"row_count": 0, "empty": True}}
    symbol_count = int(frame["code"].astype(str).nunique()) if "code" in frame.columns else 0
    if symbol_count == 0:
        return {"status": "FAIL", "publish_allowed": False,
                "checks": {"symbol_count": 0, "empty_symbols": True}}

    checks = {
        "row_count": row_count,
        "symbol_count": symbol_count,
        "expected_symbols": expected_symbols,
        "coverage": (symbol_count / expected_symbols) if expected_symbols else None,
        "input_versions": input_versions or {},
    }
    fail = False
    warn = False

    # 日期覆盖
    dates = pd.to_datetime(frame["date"], errors="coerce") if "date" in frame.columns else pd.Series(dtype="datetime64[ns]")
    max_date = str(dates.max())[:10] if dates.notna().any() else None
    checks["min_date"] = str(dates.min())[:10] if dates.notna().any() else None
    checks["max_date"] = max_date
    if expected_trade_date:
        checks["expected_trade_date"] = expected_trade_date
        checks["matches_expected"] = max_date == expected_trade_date
        if not checks["matches_expected"]:
            warn = True

    # 核心字段非空率（指标 warmup 期 NaN / 短历史缺列属正常；仅在样本充足时影响状态）
    if core_non_null_columns:
        non_null_rates = {}
        for column in core_non_null_columns:
            if column in frame.columns:
                rate = 1.0 - frame[column].isna().mean()
            else:
                rate = None
            non_null_rates[column] = round(rate, 4) if rate is not None else None
            if row_count >= 30:
                # 样本充足时，核心字段缺失或近全空视为指标计算异常
                if rate is None or rate < 0.05:
                    warn = True
        checks["core_non_null_rates"] = non_null_rates

    # 覆盖率门禁（有效基准时）
    if expected_symbols:
        ratio = symbol_count / expected_symbols
        if ratio < 0.98:
            warn = True
        if ratio < 0.9:
            fail = True

    status = "FAIL" if fail else ("WARNING" if warn else "PASS")
    publish_allowed = status == "PASS" or status == "WARNING"
    return {"status": status, "publish_allowed": publish_allowed, "checks": checks}
