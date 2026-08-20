# -*- coding: utf-8 -*-
"""A股初筛命令行入口

用法:
    python -m StockInvestmentTool.screener                        # 用 screen_rules.yaml
    python -m StockInvestmentTool.screener --max-price 35
    python -m StockInvestmentTool.screener --boards main_sh,main_sz \
        --exclude-st --no-enrich
    python -m StockInvestmentTool.screener --rules my_rules.yaml --export csv,json

命令行参数优先级高于 YAML。退出码: 0=成功(可空), 1=数据源全部失败。
"""

import argparse
import logging
import sys

import pandas as pd

from StockInvestmentTool.screener.rules import ScreenRules


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m StockInvestmentTool.screener",
        description="A股初筛：按可配置规则导出目标股票集合（规则优先级: 默认 < YAML < 命令行）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--rules", default=None, help="规则 YAML 路径（缺省用包内 screen_rules.yaml）")
    p.add_argument("--boards", default=None,
                   help="保留板块，逗号分隔: main_sh,main_sz,cyb,kcb,bse")
    p.add_argument("--exclude-boards", default=None,
                   help="剔除板块，逗号分隔（与 --boards 二选一）")
    p.add_argument("--max-price", type=float, default=None, help="股价上限(元)")
    p.add_argument("--min-price", type=float, default=None, help="股价下限(元)")
    p.add_argument("--no-exclude-st", action="store_true", help="不过滤 ST")
    p.add_argument("--no-enrich", action="store_true", help="跳过 PE/PB/市值增强")
    p.add_argument("--max-pe", type=float, default=None, help="动态PE上限")
    p.add_argument("--max-pb", type=float, default=None, help="市净率上限")
    p.add_argument("--min-total-mcap", type=float, default=None, help="总市值下限(亿)")
    p.add_argument("--min-float-mcap", type=float, default=None, help="流通市值下限(亿)")
    p.add_argument("--max-turnover", type=float, default=None, help="换手率上限(%)")
    p.add_argument("--export", default=None, help="导出格式，逗号分隔 csv,json 或 none")
    p.add_argument("--max-rows", type=int, default=None, help="导出条数上限")
    p.add_argument("--output", default=None, help="输出目录")
    p.add_argument("--universe", default=None, choices=["sina", "em", "baostock"],
                   help="股票池源")
    p.add_argument("--no-ths-summary", action="store_true", help="不导出同花顺行业汇总")
    p.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    return p.parse_args(argv)


def _csv_split(value):
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def apply_overrides(rules: ScreenRules | None, args) -> ScreenRules:
    """命令行参数覆盖 YAML（优先级: 默认 < YAML < 命令行）。"""
    rules = ScreenRules.from_yaml(args.rules)
    if args.universe:
        rules.universe_source = args.universe
    if args.boards:
        rules.boards_include = _csv_split(args.boards)
        rules.boards_exclude = None
    if args.exclude_boards:
        rules.boards_exclude = _csv_split(args.exclude_boards)
        rules.boards_include = None
    if args.max_price is not None:
        rules.price_max = args.max_price
    if args.min_price is not None:
        rules.price_min = args.min_price
    if args.no_exclude_st:
        rules.exclude_st = False
    if args.no_enrich:
        rules.enrich_enabled = False
    if args.max_pe is not None:
        rules.max_pe_ttm = args.max_pe
    if args.max_pb is not None:
        rules.max_pb = args.max_pb
    if args.min_total_mcap is not None:
        rules.min_total_mcap = args.min_total_mcap
    if args.min_float_mcap is not None:
        rules.min_float_mcap = args.min_float_mcap
    if args.max_turnover is not None:
        rules.max_turnover = args.max_turnover
    if args.export:
        rules.output_formats = [] if args.export.lower() == "none" else _csv_split(args.export)
    if args.max_rows is not None:
        rules.max_rows = args.max_rows
    if args.output:
        rules.output_dir = args.output
    if args.no_ths_summary:
        rules.ths_industry_summary = False
    return rules


def _fmt_report(report) -> str:
    lines = [
        "=" * 60,
        f"初筛完成  命中 {report.matched} 只  耗时 {report.duration_s}s",
        f"股票池源: {report.universe_source}   增强源: {report.enrich_source}",
        "-" * 60,
        "各阶段过滤统计:",
    ]
    width = max(len(s) for s, _ in report.stages)
    for stage, count in report.stages:
        lines.append(f"  {stage.ljust(width)}: {count}")
    if report.warning:
        lines.append(f"  [!] {report.warning}")
    return "\n".join(lines)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    rules = apply_overrides(None, args)

    from StockInvestmentTool.screener.pipeline import Screener
    from StockInvestmentTool.screener.export import export_result

    try:
        report = Screener(rules).run()
    except RuntimeError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1

    print(_fmt_report(report))

    if report.df.empty:
        print("命中为空，未导出文件。")
        return 0

    paths = export_result(report)
    print("-" * 60)
    print("已导出:")
    for p in paths:
        print(f"  {p}")
    if report.industry_summary is not None and not report.industry_summary.empty:
        print("-" * 60)
        print("同花顺行业热度 TOP8（仅参考）:")
        df = report.industry_summary.copy()
        for col in ("涨跌幅", "净流入", "均价"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("涨跌幅", ascending=False) if "涨跌幅" in df.columns else df
        for _, row in df.head(8).iterrows():
            line = f"  {row.get('板块','')} 涨跌幅{row.get('涨跌幅','')}% 净流入{row.get('净流入','')} 领涨{row.get('领涨股','')}"
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
