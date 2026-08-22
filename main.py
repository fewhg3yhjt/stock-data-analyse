"""StockInvestmentTool 统一入口

一键分析:
    python -m StockInvestmentTool --code sh.600900 --name 长江电力 --backtest

方案选择:
    python -m StockInvestmentTool --code sh.600900 --name 长江电力 --scheme aggressive_growth
    python -m StockInvestmentTool --list-schemes

持仓管理:
    python -m StockInvestmentTool portfolio add --code sh.600900 --name 长江电力 --shares 1000 --cost 28
    python -m StockInvestmentTool portfolio list
    python -m StockInvestmentTool portfolio show 1
    python -m StockInvestmentTool portfolio refresh
    python -m StockInvestmentTool morning-report

完整流程: 数据获取 → 技术分析 → 估值分析 → 策略回测 → Prompt生成 → LLM分析 → 报告输出
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta

from StockInvestmentTool.config import Config
from StockInvestmentTool.core.engine import AnalysisEngine, AnalysisOptions
from StockInvestmentTool.core.registry import SchemeRegistry
from StockInvestmentTool.datasource.fetcher import StockDataFetcher
from StockInvestmentTool.prompt.llm_client import LLMError

logger = logging.getLogger("StockInvestmentTool")


def setup_logging(verbose: bool = False):
    """配置日志"""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        prog="StockInvestmentTool",
        description="A股股票投资分析工具 — 自动分析、策略回测、LLM分析",
        epilog="示例: python -m StockInvestmentTool --code sh.600900 --name 长江电力 --backtest --api",
    )

    # 必选
    parser.add_argument("--code", help="股票代码 (如 sh.600900 / 600900 / sz.000001)")
    parser.add_argument("--name", help="股票名称 (如 长江电力)")

    # 时间范围
    parser.add_argument("--start",
                        help="开始日期 YYYY-MM-DD (默认1年前)")
    parser.add_argument("--end",
                        help="结束日期 YYYY-MM-DD (默认昨天)")

    # 方案
    parser.add_argument("--scheme", default="default_value",
                        help="策略方案名称 (默认 default_value)，可用 --list-schemes 查看")
    parser.add_argument("--list-schemes", action="store_true",
                        help="列出所有可用策略方案")
    parser.add_argument("--compare",
                        help="多方案对比: 逗号分隔的方案名，如 --compare default_value,aggressive_growth")

    # 回测
    parser.add_argument("--backtest", action="store_true",
                        help="执行历史回测（含参数优化）")
    parser.add_argument("--no-optimize", action="store_true",
                        help="跳过参数优化，使用默认止盈参数")
    parser.add_argument("--trail", type=float, default=0.05,
                        help="右侧移动止盈回撤阈值 (默认5%%，需配合 --no-optimize)")
    parser.add_argument("--stock-type", default="B",
                        choices=["A", "B", "C", "D"],
                        help="股票类型: A高成长/B价值白马(默认)/C强周期/D深度价值")

    # LLM
    parser.add_argument("--api", action="store_true",
                        help="自动调用 DeepSeek API 分析")
    parser.add_argument("--api-stream", action="store_true",
                        help="流式调用 LLM（长输出场景）")
    parser.add_argument("--prompt-only", action="store_true",
                        help="只生成 Prompt 文件，不调用 API")

    # 输出
    parser.add_argument("--skip-charts", action="store_true",
                        help="跳过图表生成")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="详细日志输出")

    return parser.parse_args(argv)


def list_schemes():
    """列出所有可用策略方案"""
    registry = SchemeRegistry()
    print(f"\n{'='*60}")
    print("📋 可用策略方案")
    print(f"{'='*60}")
    for s in registry.list():
        types = "/".join(s.applicable_types)
        print(f"\n  【{s.name}】v{s.version} (适用 {types})")
        print(f"    {s.description}")
        print(f"    来源: {s.source}")
    print(f"\n使用: python -m StockInvestmentTool --code <code> --name <name> --scheme <方案名>")
    print(f"{'='*60}\n")


def run_comparison(args) -> None:
    """多方案对比执行"""
    from StockInvestmentTool.comparison.runner import MultiSchemeRunner
    from StockInvestmentTool.comparison.report import write_report

    scheme_names = [s.strip() for s in args.compare.split(",") if s.strip()]
    if len(scheme_names) < 2:
        print("❌ --compare 需要至少 2 个方案，用逗号分隔，如 --compare default_value,aggressive_growth")
        sys.exit(1)

    code = StockDataFetcher.normalize_code(args.code)
    end_date = args.end or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    start_date = args.start or (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

    logger.info("开始多方案对比: %s (%s) %s ~ %s", args.name, code, start_date, end_date)
    logger.info("对比方案: %s", ", ".join(scheme_names))

    try:
        runner = MultiSchemeRunner(scheme_names)
        report = runner.compare(
            code=code, name=args.name,
            start_date=start_date, end_date=end_date,
            stock_type=args.stock_type,
        )
    except Exception as e:
        logger.exception("对比失败")
        print(f"❌ 对比失败: {e}")
        sys.exit(1)

    # 输出对比表格
    print(f"\n{'='*70}")
    print(f"📊 多方案对比：{report.name} ({report.code})  {report.start_date} ~ {report.end_date}")
    print(f"{'='*70}")
    print(f"{'方案':<22}{'收益率':>10}{'最大回撤':>10}{'夏普':>8}{'胜率':>8}{'交易':>6}")
    print("-" * 70)
    for r in report.ranking():
        m = r.metrics
        if "error" in m:
            print(f"{r.scheme_name:<22}{'失败':>10}{m['error'][:20]}")
            continue
        print(f"{r.scheme_name:<22}{m.get('total_return', 0):>9.2f}%"
              f"{m.get('max_drawdown', 0):>9.2f}%"
              f"{m.get('sharpe_ratio', 0):>8.2f}"
              f"{m.get('win_rate', 0):>7.1f}%"
              f"{m.get('trade_count', 0):>6}")
    print("-" * 70)

    # 排名摘要
    best = report.ranking()[0]
    print(f"🏆 最优方案: {best.scheme_name} (收益 {best.total_return:.2f}%)")
    for r in report.ranking()[1:]:
        diff = r.total_return - best.total_return
        print(f"   vs {r.scheme_name}: {diff:+.2f}%")

    # 生成报告 + 图表
    try:
        report_path, charts = write_report(report)
        print(f"\n📄 对比报告: {report_path}")
        for name, p in charts.items():
            print(f"   📊 {name}: {p}")
    except Exception as e:
        logger.error("对比报告生成失败: %s", e)
    print(f"{'='*70}\n")


# ═══════════════════════════════════════════════════════════════
# 持仓管理 CLI
# ═══════════════════════════════════════════════════════════════

def _get_manager():
    from StockInvestmentTool.portfolio.manager import PortfolioManager
    return PortfolioManager()


def _build_subparser(sub, rest: list[str]) -> argparse.Namespace:
    """为各子命令构建参数解析器"""
    parser = argparse.ArgumentParser(prog=f"StockInvestmentTool {sub}")
    if sub == "portfolio":
        sub_actions = parser.add_subparsers(dest="action", required=True)
        # add
        pa = sub_actions.add_parser("add", help="新建持仓")
        pa.add_argument("--code", required=True)
        pa.add_argument("--name", required=True)
        pa.add_argument("--shares", type=float, required=True)
        pa.add_argument("--cost", type=float, required=True)
        pa.add_argument("--buy-date", default=None)
        pa.add_argument("--scheme", default="default_value")
        pa.add_argument("--stock-type", default="B", choices=["A", "B", "C", "D"])
        pa.add_argument("--notes", default="")
        # list
        sub_actions.add_parser("list", help="列出持仓")
        # show
        ps = sub_actions.add_parser("show", help="持仓详情")
        ps.add_argument("id", type=int)
        # trans
        pt = sub_actions.add_parser("trans", help="记录交易")
        pt_sub = pt.add_subparsers(dest="trans_action", required=True)
        pta = pt_sub.add_parser("add", help="新增交易")
        pta.add_argument("id", type=int)
        pta.add_argument("--type", required=True, choices=["buy", "sell", "sell_all", "dividend"])
        pta.add_argument("--price", type=float, required=True)
        pta.add_argument("--shares", type=float, default=0)
        pta.add_argument("--date", default=None)
        pta.add_argument("--fee", type=float, default=0)
        pta.add_argument("--reason", default="")
        # close
        pc = sub_actions.add_parser("close", help="平仓")
        pc.add_argument("id", type=int)
        pc.add_argument("--price", type=float, required=True)
        pc.add_argument("--date", default=None)
        pc.add_argument("--reason", default="平仓")
        # refresh
        sub_actions.add_parser("refresh", help="刷新行情+建议")
        # edit
        pe = sub_actions.add_parser("edit", help="编辑/纠错")
        pe.add_argument("id", type=int)
        pe.add_argument("--shares", type=float, default=None)
        pe.add_argument("--avg-cost", type=float, default=None)
        pe.add_argument("--notes", default=None)
        # delete
        pd = sub_actions.add_parser("delete", help="删除持仓")
        pd.add_argument("id", type=int)
        # export
        sub_actions.add_parser("export", help="导出 Excel")
    elif sub == "watchlist":
        sub_actions = parser.add_subparsers(dest="action", required=True)
        wa = sub_actions.add_parser("add", help="加自选")
        wa.add_argument("--code", required=True)
        wa.add_argument("--name", default=None)
        wa.add_argument("--capital", type=float, default=0)
        wa.add_argument("--reason", default="", help="加入原因（写入 notes）")
        sub_actions.add_parser("list", help="列出自选")
        wd = sub_actions.add_parser("delete", help="删除自选")
        wd.add_argument("id", type=int)
    elif sub == "morning-report":
        parser.add_argument("--refresh", action="store_true", help="刷新行情后生成")
        parser.add_argument("--output", default=None, help="输出目录")

    return parser.parse_args(rest)


def _print_position_table(positions):
    """打印持仓表格"""
    print(f"\n{'='*80}")
    print(f"{'持仓清单':<8}{'代码':<12}{'名称':<10}{'份额':>10}{'成本':>8}{'现价':>8}{'盈亏%':>8}{'阶段':<14}")
    print("-" * 80)
    for p in positions:
        print(f"{p.id:<8}{p.stock_code:<12}{p.stock_name:<10}{p.total_shares:>10,.0f}"
              f"{p.avg_cost:>8.2f}{p.current_price:>8.2f}{p.unrealized_pnl_pct:>+7.2f}%{p.position_phase:<14}")
    print("-" * 80)


def run_management_cli(sub: str, rest: list[str]):
    """执行持仓管理子命令"""
    args = _build_subparser(sub, rest)

    if sub == "portfolio":
        mgr = _get_manager()
        if args.action == "add":
            date = args.buy_date or datetime.now().strftime("%Y-%m-%d")
            pos = mgr.add_position(args.code, args.name, args.shares, args.cost,
                                    date, args.scheme, args.stock_type, args.notes)
            print(f"✅ 建仓成功 #{pos.id}: {args.name} ({pos.stock_code}) "
                  f"{args.shares}股 @{args.cost}，方案 {args.scheme}")
        elif args.action == "list":
            _print_position_table(mgr.storage.get_open_positions())
            summary = mgr.get_summary()
            print(f"总市值 {summary['total_market_value']:,.0f} | 总盈亏 {summary['total_pnl']:+,.0f}"
                  f" ({summary['total_pnl_pct']:+.2f}%) | 可用资金 {summary['portfolio']['cash_available']:,.0f}")
        elif args.action == "show":
            detail = mgr.get_position_detail(args.id)
            p = detail["position"]
            print(f"\n持仓 #{p['id']}: {p['stock_name']} ({p['stock_code']}) [{p['position_phase']}]")
            print(f"  份额 {p['total_shares']:,.0f} | 成本 {p['avg_cost']:.2f} | 现价 {p['current_price']:.2f} | "
                  f"浮盈 {p['unrealized_pnl_pct']:+.2f}% | 已实现 {detail['realized_pnl']:+,.2f}")
            if detail["advice"]:
                a = detail["advice"]
                print(f"\n  💡 建议 [{a['advice_type']}/{a['urgency']}]: {a['reason']}")
            else:
                print("\n  💡 暂无建议（请先 refresh）")
            print("\n  交易历史:")
            for t in detail["transactions"]:
                print(f"    {t['date']} {t['trans_type']:<10} {t['shares']:>8,.0f}股 @{t['price']:.2f}"
                      f" {'盈亏 ' + format(t['pnl'], '+,.2f') if t['pnl'] else ''} {t['reason']}")
        elif args.action == "trans" and args.trans_action == "add":
            txn = mgr.record_transaction(args.id, args.type, args.price, args.shares,
                                         args.date, args.fee, args.reason)
            print(f"✅ 交易已记录 #{txn.id}: {args.type} {args.shares}股 @{args.price} 盈亏 {txn.pnl:+,.2f}")
        elif args.action == "close":
            txn = mgr.close_position(args.id, args.price, args.date, args.reason)
            print(f"✅ 持仓 #{args.id} 已平仓，实现盈亏 {txn.pnl:+,.2f}")
        elif args.action == "refresh":
            results = mgr.refresh_all()
            print(f"✅ 刷新完成，{len(results)} 个持仓")
            for r in results:
                print(f"  {r.get('stock_name', '')} ({r.get('stock_code', '')}): "
                      f"现价 {r.get('current_price', '—')} | 建议 {r.get('advice_type', '—')}")
        elif args.action == "edit":
            p = mgr.edit_position(args.id, args.shares, args.avg_cost, args.notes)
            print(f"✅ 持仓 #{args.id} 已更新: 份额 {p.total_shares} 均价 {p.avg_cost:.2f}")
        elif args.action == "delete":
            mgr.delete_position(args.id)
            print(f"✅ 持仓 #{args.id} 已删除")
        elif args.action == "export":
            from StockInvestmentTool.portfolio.export import export_to_excel
            path = export_to_excel(mgr)
            print(f"✅ Excel 已导出: {path}")

    elif sub == "watchlist":
        mgr = _get_manager()
        if args.action == "add":
            from StockInvestmentTool.datasource.fetcher import StockDataFetcher
            code = StockDataFetcher.normalize_code(args.code)
            item = mgr.add_watchlist(code, args.name or code, target_capital=args.capital,
                                     notes=args.reason, source="manual")
            print(f"✅ 已加自选 #{item.id}: {item.stock_name} ({item.stock_code})"
                  + (f"  原因: {args.reason}" if args.reason else ""))
        elif args.action == "list":
            print(f"\n{'ID':<6}{'代码':<12}{'名称':<12}{'目标仓位':>12}")
            print("-" * 50)
            for w in mgr.get_watchlist():
                print(f"{w.id:<6}{w.stock_code:<12}{w.stock_name:<12}{w.target_capital:>12,.0f}")
        elif args.action == "delete":
            mgr.delete_watchlist(args.id)
            print(f"✅ 自选 #{args.id} 已删除")

    elif sub == "morning-report":
        from StockInvestmentTool.portfolio.reporter import MorningReporter
        mgr = _get_manager()
        reporter = MorningReporter(mgr, args.output)
        path = reporter.generate(refresh=args.refresh)
        print(f"✅ 晨报已生成: {path}")


def main(argv: list[str] | None = None):
    """主入口"""
    arg_list = list(argv) if argv is not None else sys.argv[1:]

    # 持仓管理子命令分发
    if arg_list and arg_list[0] in ("portfolio", "watchlist", "morning-report"):
        setup_logging(False)
        run_management_cli(arg_list[0], arg_list[1:])
        return

    args = parse_args(arg_list)
    setup_logging(args.verbose)

    # 列出方案
    if args.list_schemes:
        list_schemes()
        return

    if not args.code or not args.name:
        print("错误: --code 和 --name 是必填参数")
        print("运行 python -m StockInvestmentTool --help 查看用法")
        sys.exit(1)

    # 多方案对比模式
    if args.compare:
        run_comparison(args)
        return

    code = StockDataFetcher.normalize_code(args.code)
    name = args.name

    # 日期范围
    end_date = args.end or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    start_date = args.start or (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

    logger.info("=" * 60)
    logger.info("StockInvestmentTool 开始分析")
    logger.info("  股票: %s (%s)", name, code)
    logger.info("  区间: %s ~ %s", start_date, end_date)
    logger.info("  方案: %s", args.scheme)
    if args.backtest:
        logger.info("  模式: 分析 + 回测")
    if args.api:
        logger.info("  模式: 自动 LLM 分析")
    logger.info("=" * 60)

    try:
        engine = AnalysisEngine(args.scheme)
    except Exception as e:
        print(f"❌ 方案加载失败: {e}")
        list_schemes()
        sys.exit(1)

    options = AnalysisOptions(
        do_backtest=args.backtest,
        do_prompt=(args.prompt_only or args.api),
        do_api=args.api,
        api_stream=args.api_stream,
        skip_charts=args.skip_charts,
        stock_type=args.stock_type,
        no_optimize=args.no_optimize,
        trail_threshold=args.trail,
    )

    try:
        result = engine.analyze(
            code=code,
            name=name,
            start_date=start_date,
            end_date=end_date,
            options=options,
        )
    except Exception as e:
        logger.exception("分析失败")
        print(f"❌ 分析失败: {e}")
        sys.exit(1)

    # ── 输出摘要 ─────────────────────────────────────────
    print(f"\n{'='*60}")
    print("✅ 分析完成!")
    print(f"   📄 报告: {result.report_path}")
    if result.chart_kline_path:
        print(f"   📊 K线图: {result.chart_kline_path}")
    if result.chart_backtest_path:
        print(f"   📊 回测图: {result.chart_backtest_path}")
    if result.chart_perf_path:
        print(f"   📊 绩效图: {result.chart_perf_path}")
    if result.prompt_path:
        print(f"   📋 Prompt: {result.prompt_path}")
    if result.llm_analysis:
        print(f"   🧠 LLM 分析完成 ({len(result.llm_analysis)} 字符)")

    # 回测摘要
    if result.backtest_metrics:
        m = result.backtest_metrics
        print(f"\n{'='*60}")
        print("📊 回测绩效")
        print(f"   💰 收益率: {m['total_return']:.2f}% | 对比持有: {m['buy_hold_return']:.2f}% → 超额 +{m['excess_return']:.2f}%")
        print(f"   📉 最大回撤: {m['max_drawdown']:.2f}% | 夏普: {m['sharpe_ratio']}")
        print(f"   🔄 交易 {m['trade_count']} 次 | 胜率 {m['win_rate']:.1f}%")

        best = result.backtest_result.get("best_params",
                                          result.backtest_result.get("custom_params", {}))
        if best:
            print(f"   ⚙️ 回测参数: 右侧回撤 {best.get('trail_threshold', 0)*100:.0f}% + 买入偏移 {best.get('offset', 0)*100:+.0f}%")
        top = result.backtest_result.get("top_combos", [])
        if top:
            print("   📊 Top 5 参数组合:")
            for i, c in enumerate(top[:5], 1):
                print(f"      {i}. 回撤{c['trail_threshold']*100:.0f}% +偏移{c['offset']*100:+.0f}% → {c['return']:.2f}%")
        print(f"{'='*60}")


if __name__ == "__main__":
    main()
