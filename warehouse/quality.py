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


def _industry_quality(path, dataset_name: str, expected_symbols: int | None = None) -> dict:
    frame = pd.read_parquet(path)
    config = load_dataset_config(dataset_name)["quality"]
    keys = load_dataset_config(dataset_name)["dataset"]["primary_keys"]
    required = set(keys)
    missing = sorted(required - set(frame.columns))
    if missing or frame.empty:
        return {"status": "FAIL", "publish_allowed": False, "checks": {"missing_columns": missing, "row_count": len(frame)}}
    duplicate = int(frame.duplicated(keys).sum())
    if dataset_name == "industry_membership":
        valid = frame["industry_code"].astype(str).ne("") & frame["industry_name"].astype(str).ne("")
    else:
        valid = frame["industry_id"].astype(str).ne("") & frame["industry_name"].astype(str).ne("")
        numeric = {name: pd.to_numeric(frame[name], errors="coerce") for name in ("open", "high", "low", "close", "volume", "amount")}
        valid &= numeric["close"].gt(0) & numeric["high"].ge(numeric["low"]) & numeric["volume"].ge(0) & numeric["amount"].ge(0)
    coverage = len(frame[keys[1]].unique()) / expected_symbols if expected_symbols else None
    fail = duplicate > 0 or int((~valid).sum()) > 0 or (coverage is not None and coverage < config["coverage"]["warning_min"])
    status = "FAIL" if fail else ("PASS" if coverage is None or coverage >= config["coverage"]["pass_min"] else "WARNING")
    return {"status": status, "publish_allowed": status != "FAIL" and (status == "PASS" or config["publish_warning"]),
            "checks": {"duplicate_primary_keys": duplicate, "invalid_rows": int((~valid).sum()),
                       "row_count": len(frame), "coverage": coverage, "expected_symbols": expected_symbols}}


def check_industry_membership(path, expected_symbols: int | None = None) -> dict:
    return _industry_quality(path, "industry_membership", expected_symbols)


def check_ths_industry_membership(path, expected_symbols: int | None = None, *,
                                  expected_industries: int | None = None,
                                  expected_rows: int | None = None,
                                  expected_source_commit: str | None = None) -> dict:
    frame = pd.read_parquet(path)
    config = load_dataset_config("ths_industry_membership")["quality"]
    keys = ["snapshot_date", "industry_id", "code"]
    required = set(keys) | {"industry_name", "stock_name", "source_commit", "source", "captured_at"}
    missing = sorted(required - set(frame.columns))
    if missing or frame.empty:
        return {"status": "FAIL", "publish_allowed": False,
                "checks": {"missing_columns": missing, "row_count": len(frame)}}
    duplicate = int(frame.duplicated(keys).sum())
    valid = (frame["snapshot_date"].astype(str).str[:10].str.fullmatch(r"\d{4}-\d{2}-\d{2}") &
             frame["industry_id"].astype(str).str.strip().ne("") &
             frame["industry_name"].astype(str).str.strip().ne("") &
             frame["code"].astype(str).str.strip().ne("") &
             frame["stock_name"].astype(str).str.strip().ne("") &
             frame["source_commit"].astype(str).str.len().eq(40))
    count = len(frame)
    industry_count = int(frame["industry_id"].astype(str).nunique())
    relation_count = int(frame[keys].drop_duplicates().shape[0])
    expected_industries = expected_industries if expected_industries is not None else expected_symbols
    coverage = (industry_count / expected_industries if expected_industries else None)
    baseline = config.get("source_baseline", {})
    source_commits = frame["source_commit"].astype(str).str.strip()
    source_commit = source_commits.iloc[0] if len(frame) else ""
    baseline_applies = expected_source_commit is not None and source_commit == expected_source_commit
    baseline_rows = expected_rows if baseline_applies else None
    baseline_industries = expected_industries if baseline_applies else None
    baseline_row_bad = baseline_rows is not None and count != baseline_rows
    baseline_industry_bad = baseline_industries is not None and industry_count != baseline_industries
    mixed_source_bad = source_commits.nunique() != 1
    fail = (duplicate > 0 or int((~valid).sum()) > 0 or
            (coverage is not None and coverage < config["coverage"]["warning_min"]) or
            baseline_row_bad or baseline_industry_bad or mixed_source_bad)
    status = "FAIL" if fail else ("PASS" if coverage is None or coverage >= config["coverage"]["pass_min"] else "WARNING")
    return {"status": status, "publish_allowed": status != "FAIL" and (status == "PASS" or config["publish_warning"]),
            "checks": {"duplicate_primary_keys": duplicate, "invalid_rows": int((~valid).sum()),
                       "row_count": count, "industry_count": industry_count,
                       "relation_count": relation_count, "coverage": coverage,
                       "expected_industries": expected_industries,
                       "baseline_applies": baseline_applies,
                       "baseline_expected_rows": baseline_rows,
                       "baseline_expected_industries": baseline_industries,
                       "baseline_row_count_mismatch": baseline_row_bad,
                       "baseline_industry_count_mismatch": baseline_industry_bad,
                       "mixed_source_commits": mixed_source_bad,
                       "source_baseline": baseline}}


def check_industry_daily(path, expected_symbols: int | None = None) -> dict:
    return _industry_quality(path, "industry_daily", expected_symbols)


def check_industry_features_daily(path, *, expected_industries: int | None = None,
                                  expected_as_of: str | None = None) -> dict:
    """Quality gate specific to the industry-feature grain and as-of boundary."""
    frame = pd.read_parquet(path) if Path(path).exists() else pd.DataFrame()
    required = {"date", "industry_code", "industry_name", "industry_classification",
                "member_count", "valid_count", "up_count", "down_count", "up_ratio",
                "return_1d", "return_3d", "return_5d", "return_10d", "return_20d",
                "amount", "amount_ma5", "amount_ma20", "amount_ratio",
                "rank_1d", "rank_5d", "rank_20d", "leader_code", "leader_return",
                "leader_amount", "industry_score", "industry_state", "state_reason"}
    missing = sorted(required - set(frame.columns))
    checks = {"missing_columns": missing, "row_count": int(len(frame))}
    if missing or frame.empty:
        return {"status": "FAIL", "publish_allowed": False, "checks": checks}
    keys = ["date", "industry_code", "industry_classification"]
    duplicate = int(frame.duplicated(keys).sum())
    valid_fields = (frame["industry_code"].astype(str).str.strip().ne("") &
                    frame["industry_name"].astype(str).str.strip().ne("") &
                    frame["industry_classification"].astype(str).str.strip().ne("") &
                    pd.to_numeric(frame["member_count"], errors="coerce").ge(0) &
                    pd.to_numeric(frame["valid_count"], errors="coerce").ge(0))
    dates = pd.to_datetime(frame["date"], errors="coerce")
    max_date = str(dates.max())[:10] if dates.notna().any() else None
    coverage = (frame["industry_code"].astype(str).nunique() / expected_industries
                if expected_industries else None)
    checks.update({"duplicate_primary_keys": duplicate, "invalid_industry_rows": int((~valid_fields).sum()),
                   "industry_count": int(frame["industry_code"].astype(str).nunique()),
                   "expected_industries": expected_industries, "coverage": coverage,
                   "min_date": str(dates.min())[:10] if dates.notna().any() else None,
                   "max_date": max_date, "expected_as_of": expected_as_of})
    # ``as_of`` may be a weekend/holiday; the invariant is that the output
    # contains no data after it, not that a non-trading date must be present.
    as_of_bad = expected_as_of is not None and (
        max_date is None or pd.Timestamp(max_date) > pd.Timestamp(expected_as_of)
    )
    invalid_state = frame["industry_state"].astype(str).isin({"strong", "neutral", "weak", "insufficient_data"}) == False
    fail = duplicate > 0 or int((~valid_fields).sum()) > 0 or as_of_bad or (coverage is not None and coverage < 0.9)
    fail |= int(invalid_state.sum()) > 0
    return {"status": "FAIL" if fail else ("WARNING" if coverage is not None and coverage < 0.98 else "PASS"),
            "publish_allowed": not fail, "checks": checks}


def check_industry_rotation_daily(path, *, expected_as_of: str | None = None) -> dict:
    frame = pd.read_parquet(path) if Path(path).exists() else pd.DataFrame()
    required = {"date", "industry_id", "industry_name", "classification", "strength_score",
                "rotation_score", "rank_3d", "rank_5d", "rank_20d", "rotation_rank", "rotation_rank_1d_ago", "rotation_rank_3d_ago", "rotation_rank_5d_ago", "rank_3d_change",
                "relative_return_3d", "relative_return_5d", "relative_return_10d", "strength_level", "rotation_heat", "rotation_acceleration", "deterioration",
                "opportunity_score", "transition_type", "stage", "previous_stage", "stage_days", "transition", "reason", "advice"}
    missing = sorted(required - set(frame.columns))
    checks = {"missing_columns": missing, "row_count": int(len(frame))}
    if missing or frame.empty:
        return {"status": "FAIL", "publish_allowed": False, "checks": checks}
    keys = ["date", "industry_id", "classification"]
    duplicate = int(frame.duplicated(keys).sum())
    dates = pd.to_datetime(frame["date"], errors="coerce")
    max_date = str(dates.max())[:10] if dates.notna().any() else None
    stages = {"DORMANT", "WARMING", "STARTING", "RISING", "CLIMAX", "FADING", "COLD"}
    invalid_stage = int((~frame["stage"].astype(str).isin(stages)).sum())
    bad_days = int((pd.to_numeric(frame["stage_days"], errors="coerce") < 1).sum())
    as_of_bad = expected_as_of is not None and (max_date is None or pd.Timestamp(max_date) > pd.Timestamp(expected_as_of))
    checks.update({"duplicate_primary_keys": duplicate, "invalid_stage": invalid_stage,
                   "invalid_stage_days": bad_days, "max_date": max_date, "expected_as_of": expected_as_of})
    fail = duplicate > 0 or invalid_stage > 0 or bad_days > 0 or as_of_bad
    return {"status": "FAIL" if fail else "PASS", "publish_allowed": not fail, "checks": checks}
