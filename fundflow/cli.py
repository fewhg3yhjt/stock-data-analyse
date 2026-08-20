# -*- coding: utf-8 -*-
"""A股资金流分析命令行入口

用法:
    python -m StockInvestmentTool.fundflow                        # 即时 + 3日趋势
    python -m StockInvestmentTool.fundflow --trend-days 3,5       # 加 5 日对比
    python -m StockInvestmentTool.fundflow --sections stock       # 只看个股
    python -m StockInvestmentTool.fundflow --no-export            # 只打印不落盘

退出码: 0=成功, 1=数据拉取失败。
"""

import argparse
import logging
import sys

import pandas as pd

from StockInvestmentTool.fundflow import analysis, sources
from StockInvestmentTool.fundflow.export import export_result


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m StockInvestmentTool.fundflow",
        description="A股资金流分析：同花顺行业/概念/个股 × 即时/多日趋势（短线视角）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--sections", default="industry,concept,stock",
                   help="分析范围: industry,concept,stock 逗号分隔")
    p.add_argument("--trend-days", default="3",
                   help="多日趋势对比周期: 3,5,10,20（可多选）")
    p.add_argument("--top", type=int, default=15, help="榜单条数")
    p.add_argument("--export", action="store_true", default=True,
                   help="导出 CSV/JSON（默认开）")
    p.add_argument("--no-export", dest="export", action="store_false",
                   help="只打印不落盘")
    p.add_argument("--output", default=None, help="输出目录")
    p.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    return p.parse_args(argv)


def _rows(df: pd.DataFrame) -> list[dict]:
    return df.to_dict("records")


def _fmt_flow(v):
    return "—" if v is None else f"{v:.2f}亿"


def _print_table(title, df, cols, top=15):
    print(f"\n{title}")
    print("-" * 80)
    if df is None or df.empty:
        print("  (空)")
        return
    show = {c: df[c] for c in cols if c in df.columns}
    out = pd.DataFrame(show)
    fmt = {}
    for c in ("net", "net_days", "in", "out"):
        if c in out.columns:
            fmt[c] = _fmt_flow
    print(out.head(top).to_string(index=False, formatters=fmt))


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    sections = [s.strip() for s in args.sections.split(",") if s.strip()]
    trend_days = [d.strip() for d in args.trend_days.split(",") if d.strip()]
    meta = {"generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sections": sections, "trend_days": trend_days,
            "trend_period": f"{trend_days[0]}d" if trend_days else "3d",
            "data_source": "同花顺资金流"}

    payload: dict = {"meta": meta}
    try:
        # ── 个股（全市场，先拉，用于大盘概况）──
        if "stock" in sections:
            stk_now = sources.fetch_stock("now")
            overview = analysis.market_overview(stk_now)
            print("=" * 80)
            print("大盘概况（同花顺口径，全市场个股汇总）")
            print("=" * 80)
            for k, v in overview.items():
                print(f"  {k}: {v}")

            stk_days = None
            if trend_days:
                stk_days = sources.fetch_stock(f"{trend_days[0]}d")
            stk_analysis = analysis.stock_analysis(stk_now, stk_days, top=args.top)
            payload["stock"] = {"now": _rows(stk_now),
                                "analysis": {k: _rows(v) for k, v in stk_analysis.items()}}

            print(f"\n>>> 个股主力净流入 TOP{args.top}")
            _print_table("", stk_analysis["净流入榜"],
                         ["code", "name", "price", "chg", "net", "net_days", "trend", "turnover"], args.top)
            print(f"\n>>> 个股净流出 TOP{args.top}")
            _print_table("", stk_analysis["净流出榜"],
                         ["code", "name", "chg", "net", "net_days", "trend"], args.top)
            print(f"\n>>> 持续流入 TOP{args.top}（今日+近{trend_days[0]}日均净流入）")
            _print_table("", stk_analysis["持续流入榜"],
                         ["code", "name", "chg", "net", "net_days", "trend", "turnover"], args.top)

        # ── 行业 / 概念 ──
        for kind in ("industry", "concept"):
            if kind not in sections:
                continue
            now = sources.fetch_sector(kind, "now")
            days = sources.fetch_sector(kind, f"{trend_days[0]}d") if trend_days else None
            tab = analysis.sector_analysis(now, days, top=args.top)
            payload[kind] = {"now": _rows(now), "days": _rows(days) if days is not None else []}
            print(f"\n{'#'*80}\n# {kind} 板块资金流（即时净额排序, 近{trend_days[0]}日趋势）\n{'#'*80}")
            _print_table("", tab, ["name", "chg", "net", "net_days", "trend", "leader", "leader_chg"], args.top)
    except RuntimeError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1

    if args.export:
        try:
            paths = export_result(payload, args.output)
            print("\n已导出:")
            for p in paths:
                print(f"  {p}")
        except Exception as e:
            print(f"[导出失败] {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
