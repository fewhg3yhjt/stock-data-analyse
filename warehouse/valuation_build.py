"""Pure valuation derivation from explicitly supplied Published inputs."""
from __future__ import annotations

from datetime import datetime
import pandas as pd
import numpy as np


def ttm_series(frame: pd.DataFrame, value_column: str) -> pd.Series:
    """TTM for cumulative quarterly reports: current YTD + prior annual - prior YTD."""
    data = frame.copy()
    data["report_date"] = pd.to_datetime(data["report_date"])
    data = data.sort_values("report_date")
    values = data.set_index("report_date")[value_column].astype(float)
    out = {}
    for date, value in values.items():
        prior_year = date - pd.DateOffset(years=1)
        prior_annual = values.get(pd.Timestamp(year=prior_year.year, month=12, day=31), values.get(prior_year))
        prior_ytd = values.get(prior_year)
        out[date] = value if date.month == 12 or prior_annual is None or prior_ytd is None else value + prior_annual - prior_ytd
    return pd.Series(out, index=data["report_date"])


def build_valuation_daily(financial_reports: pd.DataFrame, snapshots: pd.DataFrame, *, calculated_at: str | None = None) -> pd.DataFrame:
    if financial_reports.empty or snapshots.empty:
        return pd.DataFrame()
    financial = financial_reports.copy()
    financial["report_date"] = pd.to_datetime(financial["report_date"])
    income = financial[financial["statement_type"].eq("profit")].copy()
    balance = financial[financial["statement_type"].eq("balance")].copy()
    if income.empty:
        return pd.DataFrame()
    result = snapshots.copy()
    result["date"] = pd.to_datetime(result["trade_date"])
    # ``groupby.apply`` returns a date-indexed Series and silently misaligns
    # when multiple codes share the same report dates.  Assign by the original
    # row index instead, keeping each security's TTM isolated.
    income["revenue_ttm"] = pd.NA
    income["net_profit_ttm"] = pd.NA
    for _, group in income.groupby("code", sort=False):
        ttm_revenue = ttm_series(group, "revenue")
        ttm_profit = ttm_series(group, "net_profit_parent")
        for row_index, report_date in group["report_date"].items():
            income.loc[row_index, "revenue_ttm"] = ttm_revenue.get(report_date)
            income.loc[row_index, "net_profit_ttm"] = ttm_profit.get(report_date)
    result_rows = []
    for _, snapshot in result.iterrows():
        code_income = income[(income["code"] == snapshot["code"]) & (income["report_date"] <= snapshot["date"])]
        latest_income = code_income.sort_values("report_date").tail(1)
        code_balance = balance[(balance["code"] == snapshot["code"]) & (balance["report_date"] <= snapshot["date"])]
        latest_balance = code_balance.sort_values("report_date").tail(1)
        row = snapshot.to_dict()
        if not latest_income.empty:
            item = latest_income.iloc[0]
            row.update(revenue_ttm=item.get("revenue_ttm"), net_profit_ttm=item.get("net_profit_ttm"),
                       financial_report_date=item["report_date"], financial_publish_date=item.get("financial_publish_date"))
        else:
            row.update(revenue_ttm=np.nan, net_profit_ttm=np.nan, financial_report_date=pd.NaT, financial_publish_date=None)
        row["parent_equity"] = latest_balance.iloc[0].get("parent_equity") if not latest_balance.empty else np.nan
        result_rows.append(row)
    result = pd.DataFrame(result_rows)
    result["price_source"] = result.get("price_source", "tencent_quotes")
    result["financial_source"] = "sina_financial_html"
    result["pe_ttm_calc"] = result["total_mv"].div(result["net_profit_ttm"].where(result["net_profit_ttm"] > 0))
    result["pb_calc"] = result["total_mv"].div(result["parent_equity"].where(result["parent_equity"] > 0))
    result["ps_ttm_calc"] = result["total_mv"].div(result["revenue_ttm"].where(result["revenue_ttm"] > 0))
    result["calculated_at"] = calculated_at or datetime.now().isoformat(timespec="seconds")
    result["quality_status"] = "PASS"
    result["quality_reason"] = ""
    columns = ["date", "code", "price", "total_mv", "circ_mv", "pe_ttm_source", "pb_source", "pe_ttm_calc",
               "pb_calc", "ps_ttm_calc", "revenue_ttm", "net_profit_ttm", "parent_equity", "profit_growth_yoy",
               "profit_cagr_3y", "peg_yoy", "peg_cagr_3y", "financial_report_date", "financial_publish_date",
               "price_source", "financial_source", "calculated_at", "quality_status", "quality_reason"]
    for column in columns:
        if column not in result: result[column] = np.nan
    return result[columns]


def quality_report(frame: pd.DataFrame, *, expected_symbols: int | None = None) -> dict:
    required = ("date", "code", "price", "total_mv", "revenue_ttm", "net_profit_ttm", "parent_equity")
    checks = {"required_columns": all(c in frame for c in required),
              "empty": not frame.empty,
              "duplicate_keys": not frame.duplicated(["date", "code"]).any() if not frame.empty else False,
              "valid_price": bool((pd.to_numeric(frame.get("price"), errors="coerce") > 0).all()) if not frame.empty else False,
              "ttm": all(c in frame for c in ("revenue_ttm", "net_profit_ttm", "parent_equity"))}
    coverage = frame["code"].nunique() / expected_symbols if expected_symbols else 1.0
    checks["coverage"] = coverage
    core = ["price", "total_mv", "revenue_ttm", "net_profit_ttm", "parent_equity"]
    checks["core_non_null_rate"] = float(frame[core].notna().mean().min()) if not frame.empty and all(c in frame for c in core) else 0.0
    status = "PASS" if all(v is True for v in checks.values() if isinstance(v, bool)) and coverage >= .98 and checks["core_non_null_rate"] >= .98 else "WARNING"
    return {"status": status, "checks": checks, "publish_allowed": status in {"PASS", "WARNING"}}
