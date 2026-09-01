#!/usr/bin/env python3
"""验证 Tushare 板块数据接口是否满足轮动观测的最低要求。

该脚本是只读、隔离验证工具，不接入 Warehouse，也不修改项目数据。
需要环境变量 TUSHARE_TOKEN，并安装 tushare：

    TUSHARE_TOKEN=... python scripts/verify_tushare_sector_data.py

脚本会分别验证：交易日、申万行业目录、行业成分、行业指数日线，以及
同花顺板块目录/成分/日线（如果账户和接口版本支持）。部分 Tushare
接口在不同账户权限下不可用，因此每个检查独立记录结果。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from typing import Any, Callable


def _parse_args() -> argparse.Namespace:
    today = date.today()
    default_start = today - timedelta(days=30)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=default_start.strftime("%Y%m%d"),
                        help="历史起始日期，格式 YYYYMMDD")
    parser.add_argument("--end", default=today.strftime("%Y%m%d"),
                        help="历史结束日期，格式 YYYYMMDD")
    parser.add_argument("--industry", default="",
                        help="指定行业代码，例如 801010.SI；默认取目录第一条")
    parser.add_argument("--ths-symbol", default="",
                        help="指定同花顺板块代码；默认取目录第一条")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="仅输出 JSON 结果")
    return parser.parse_args()


def _frame_summary(frame: Any) -> dict[str, Any]:
    if frame is None:
        return {"status": "empty", "rows": 0, "columns": []}
    columns = [str(column) for column in getattr(frame, "columns", [])]
    rows = int(len(frame))
    result: dict[str, Any] = {
        "status": "ok" if rows else "empty",
        "rows": rows,
        "columns": columns,
    }
    if rows and "trade_date" in columns:
        values = frame["trade_date"].dropna().astype(str)
        if len(values):
            result["date_min"] = values.min()
            result["date_max"] = values.max()
    return result


def _call(name: str, fn: Callable[[], Any], results: dict[str, Any]) -> Any:
    try:
        value = fn()
    except Exception as exc:  # API permissions and network failures are expected here.
        results[name] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        return None
    results[name] = _frame_summary(value)
    return value


def _first_value(frame: Any, column: str) -> str:
    if frame is None or len(frame) == 0 or column not in frame.columns:
        return ""
    value = frame.iloc[0][column]
    return "" if value is None else str(value)


def _check_tushare(args: argparse.Namespace) -> dict[str, Any]:
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        return {"status": "skipped", "reason": "未设置 TUSHARE_TOKEN"}
    try:
        import tushare as ts
    except ImportError:
        return {"status": "skipped", "reason": "未安装 tushare，请先安装依赖"}

    pro = ts.pro_api(token)
    results: dict[str, Any] = {}
    calendar = _call(
        "trade_cal",
        lambda: pro.trade_cal(exchange="SSE", start_date=args.start,
                               end_date=args.end, is_open="1"),
        results,
    )

    sw_catalog = _call(
        "sw_index_catalog",
        lambda: pro.index_classify(level="L1", src="SW2021"),
        results,
    )
    sw_code = args.industry or _first_value(sw_catalog, "index_code")
    results["sw_selected_index_code"] = sw_code
    if sw_code:
        sw_members = _call(
            "sw_index_member",
            lambda: pro.index_member(index_code=sw_code),
            results,
        )
        _call(
            "sw_index_daily",
            lambda: pro.sw_daily(ts_code=sw_code, start_date=args.start,
                                 end_date=args.end),
            results,
        )
        if sw_members is not None and len(sw_members):
            results["sw_member_code_column"] = next(
                (column for column in ("con_code", "ts_code", "股票代码")
                 if column in sw_members.columns),
                None,
            )
    else:
        results["sw_index_member"] = {"status": "skipped", "reason": "未获得行业代码"}
        results["sw_index_daily"] = {"status": "skipped", "reason": "未获得行业代码"}

    # These endpoints are not available to every account. They are deliberately
    # independent checks so one permission failure does not hide other results.
    ths_catalog = _call(
        "ths_index_catalog",
        lambda: pro.ths_index(exchange="A", type="N"),
        results,
    )
    ths_code = args.ths_symbol or _first_value(ths_catalog, "ts_code")
    results["ths_selected_index_code"] = ths_code
    if ths_code:
        ths_members = _call(
            "ths_index_member",
            lambda: pro.ths_member(ts_code=ths_code),
            results,
        )
        _call(
            "ths_index_daily",
            lambda: pro.ths_daily(ts_code=ths_code, start_date=args.start,
                                  end_date=args.end),
            results,
        )
        if ths_members is not None and len(ths_members):
            results["ths_member_code_column"] = next(
                (column for column in ("con_code", "ts_code", "股票代码")
                 if column in ths_members.columns),
                None,
            )
    else:
        results["ths_index_member"] = {"status": "skipped", "reason": "未获得板块代码"}
        results["ths_index_daily"] = {"status": "skipped", "reason": "未获得板块代码"}

    results["trade_days_observed"] = (
        int(len(calendar)) if calendar is not None else 0
    )
    successful_data = sum(
        1 for name, value in results.items()
        if name.endswith(("daily", "member", "catalog"))
        and isinstance(value, dict) and value.get("status") == "ok"
    )
    results["status"] = "ok" if successful_data else "failed"
    return results


def main() -> int:
    args = _parse_args()
    result = {
        "tool": "tushare_sector_data_verification",
        "start": args.start,
        "end": args.end,
        "tushare": _check_tushare(args),
    }
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"验证区间: {args.start} - {args.end}")
        check = result["tushare"]
        print(f"Tushare 总状态: {check.get('status')}")
        if check.get("reason"):
            print(f"原因: {check['reason']}")
        for name, value in check.items():
            if not isinstance(value, dict) or name == "status":
                continue
            status = value.get("status", "info")
            detail = f"rows={value.get('rows', 0)}"
            if value.get("error"):
                detail = value["error"]
            elif value.get("reason"):
                detail = value["reason"]
            print(f"- {name}: {status} ({detail})")
    return 0 if result["tushare"].get("status") in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
