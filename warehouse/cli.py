# -*- coding: utf-8 -*-
"""全量数据仓库 CLI — 离线全量采集 / 因子计算 / 全市场扫描 / 在线快照

用法:
    python -m StockInvestmentTool.warehouse init --start YYYY-MM-DD --end YYYY-MM-DD [--include-index]
    python -m StockInvestmentTool.warehouse sync    [--start YYYY-MM-DD] [--max-symbols N]
    python -m StockInvestmentTool.warehouse scan    --start YYYY-MM-DD --end YYYY-MM-DD [--where SQL] [--limit N]
    python -m StockInvestmentTool.warehouse online  [--codes c1,c2] [--day YYYY-MM-DD]
    python -m StockInvestmentTool.warehouse status
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta

logger = logging.getLogger("warehouse")


def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")


def cmd_init(args):
    """首建：同步全市场代码清单 + 显式日期范围日线。"""
    from StockInvestmentTool.warehouse.collector import MarketCollector
    c = MarketCollector()
    n = c.sync_instruments(include_etf=True, include_index=args.include_index)
    print(f"✅ 标的清单已入库: {n} 条")
    if not args.start or not args.end:
        raise ValueError("init 必须显式传入 --start 和 --end")
    start, end = args.start, args.end
    res = c.sync_daily(start_date=start, end_date=end, include_etf=True,
                       include_index=args.include_index, max_symbols=args.max_symbols,
                       source="baostock", target="raw:baostock")
    print(f"✅ 日线同步: +{res['added_rows']} 行, 失败 {len(res['failed'])}")
    if res["failed"]:
        print(f"   失败代码(前10): {res['failed'][:10]}")


def cmd_sync(args):
    """增量同步日线（收盘后每日跑）。"""
    from StockInvestmentTool.warehouse.collector import MarketCollector
    c = MarketCollector()
    if not args.start or not args.end:
        raise ValueError("sync 必须显式传入 --start 和 --end")
    res = c.sync_daily(start_date=args.start, end_date=args.end, include_etf=True,
                       include_index=args.include_index, max_symbols=args.max_symbols,
                       flush_every=args.flush_every, source=args.source,
                       target=args.target)
    print(f"✅ 日线增量: +{res['added_rows']} 行, 失败 {len(res['failed'])}, 耗时 {res['elapsed_sec']}s")


def cmd_scan(args):
    """全市场指标扫描（读取 indicators 分区）。"""
    from StockInvestmentTool.warehouse.storage import Warehouse
    from StockInvestmentTool.warehouse.indicators_build import IndicatorsBuilder
    w = Warehouse()
    months = w.available_months("indicator")
    if not months:
        print("⚠️ 无指标分区（先运行 indicators）")
        return
    import duckdb
    files = [str(w.indicator_dir / f"{m}.parquet") for m in months]
    fl = "[" + ",".join("'" + f.replace("'", "''") + "'" for f in files) + "]"
    sql = f"""
        WITH latest AS (
            SELECT * FROM read_parquet({fl})
            QUALIFY ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) = 1
        )
        SELECT * FROM latest
        WHERE date BETWEEN ? AND ?
        {('AND (' + args.where + ')') if args.where else ''}
        ORDER BY {args.order_by}
        LIMIT {int(args.limit)}
    """
    con = duckdb.connect()
    try:
        rows = con.execute(sql, [args.start, args.end]).fetchall()
        cols = [d[0] for d in con.description]
        hits = [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()
    if not hits:
        print("⚠️ 无结果（检查日期区间是否有数据、where 条件是否正确）")
        return
    print(f"{'代码':<12}{'日期':<12}{'收盘':>8}{'量比':>8}{'5日涨幅%':>10}")
    print("-" * 52)
    for h in hits:
        print(f"{h.get('code',''):<12}{str(h.get('date',''))[:10]:<12}"
              f"{h.get('close',0):>8.2f}{h.get('vol_ratio',0):>8.2f}"
              f"{h.get('ret_5d',0):>10.2f}")
    print(f"\n共 {len(hits)} 条")


def cmd_online(args):
    """观察池盘中低频快照（腾讯报价，不封 IP）。"""
    from StockInvestmentTool.warehouse.online import collect_online_snapshot
    day = args.day or datetime.now().strftime("%Y-%m-%d")
    codes = [c.strip() for c in (args.codes or "").split(",") if c.strip()]
    path = collect_online_snapshot(codes=codes, day=day)
    print(f"✅ 在线快照已写入: {path}")


def cmd_reset(args):
    """清理仓库数据（破坏性）。"""
    from StockInvestmentTool.warehouse.storage import Warehouse
    kinds = [k for k in (args.kinds or "daily,factor,online").split(",") if k]
    print(f"⚠️ 即将清空: {kinds}")
    if not args.force:
        confirm = input("确认清空? 输入 yes 继续: ").strip().lower()
        if confirm != "yes":
            print("已取消")
            return
    w = Warehouse()
    result = w.reset(kinds)
    print(f"✅ 已清空: {result}")


def cmd_process(args):
    """把贴源层 raw/* 加工为 daily/ 完整宽表。"""
    from StockInvestmentTool.warehouse.process import ProcessEngine
    months = [m.strip() for m in (args.months or "").split(",") if m.strip()] or None
    pe = ProcessEngine()
    result = pe.build_all(months=months)
    if result:
        print(f"✅ 加工完成: {len(result)} 个月, 总 {sum(result.values())} 行")
    else:
        print("⚠️ 贴源层无数据可加工（先跑 sync --target raw:tencent）")


def cmd_backfill(args):
    """单点回补：从东财补历史 PE/PB 到加工层。"""
    from StockInvestmentTool.warehouse.backfill import ValuationBackfill
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    if not codes:
        print("❌ 需要 --codes（逗号分隔）")
        return
    vb = ValuationBackfill()
    if len(codes) == 1:
        r = vb.backfill(codes[0], args.start, args.end, reprocess=True)
        print(f"✅ 回补 {codes[0]}: {r['rows']} 行, {r['months']} 个月")
    else:
        r = vb.backfill_many(codes, args.start, args.end, reprocess=True)
        print(f"✅ 批量回补: {r['rows']} 行, 失败 {r['failed']}")


def cmd_fundamentals(args):
    """采集财务史；行业旧命令已迁移至 industry membership。"""
    from StockInvestmentTool.warehouse.fundamentals_collect import FundamentalsCollector
    fc = FundamentalsCollector()
    result = {}
    if args.kind == "industry":
        raise RuntimeError("fundamentals --kind industry 已废弃；请使用 industry membership")
    if args.kind in ("financial", "all"):
        r = fc.collect_fundamentals(max_symbols=args.max_symbols)
        result["financial"] = r
        print(f"✅ 财务史采集: 完成 {r['done']}, 跳过 {r['skipped']}, 失败 {r['failed']}")
    return result


def cmd_status(args):
    """仓库状态总览。"""
    from StockInvestmentTool.warehouse.storage import Warehouse
    w = Warehouse()
    print("=" * 56)
    print("📦 全量数据仓库状态")
    print("=" * 56)
    print(f"仓库目录: {w.base_dir}")
    print(f"标的数:   {len(w.all_codes())}")
    daily = w.available_months("daily")
    ind = w.available_months("indicator")
    print(f"日线分区: {len(daily)} 个月  {daily[:3]}{'...' if len(daily)>3 else ''}")
    print(f"指标分区: {len(ind)} 个月  {ind[:3]}{'...' if len(ind)>3 else ''}")
    if daily:
        last = w.read_daily(daily[-1])
        if last is not None and len(last):
            print(f"最新数据: {last['date'].max().date()} ({len(last)} 行)")
    online = sorted(p.name for d in w.online_dir.iterdir() if d.is_dir()
                    for p in w.online_dir.joinpath(d.name).glob("*.csv"))
    print(f"在线快照: {len(online)} 份")
    print("=" * 56)


def cmd_export_public_daily(args):
    """Export configured static public JSON files from Published stock_daily."""
    from StockInvestmentTool.warehouse.public_export import export_public_daily
    from StockInvestmentTool.warehouse.storage import Warehouse

    codes = tuple(item.strip() for item in args.codes.split(",") if item.strip()) if args.codes else None
    result = export_public_daily(Warehouse(), codes=codes)
    print(result)


def cmd_industry(args):
    from StockInvestmentTool.warehouse.industry import IndustryCollector, stage_and_publish_industry_batch
    from StockInvestmentTool.warehouse.storage import Warehouse
    warehouse = Warehouse()
    warehouse.metadata.register_dataset("industry_membership")
    warehouse.metadata.register_dataset("industry_daily")
    c = IndustryCollector(warehouse, interval=args.interval, retries=args.retries, backoff=args.backoff)
    if args.kind == "membership":
        result = c.collect_membership(max_symbols=args.max_symbols, snapshot_date=args.snapshot_date, refresh=args.refresh)
    else:
        if not args.start or not args.end:
            raise ValueError("industry daily 需要 --start 和 --end")
        result = c.collect_daily(start_date=args.start, end_date=args.end)
    if result.get("raw_batch_id"):
        dataset = "industry_membership" if args.kind == "membership" else "industry_daily"
        published = stage_and_publish_industry_batch(
            warehouse, dataset_name=dataset, batch_id=result["raw_batch_id"],
            expected_symbols=result.get("expected_symbols") or result.get("success"),
        )
        result["published"] = published
    print(result)


def cmd_ths_industry_import(args):
    from StockInvestmentTool.warehouse.storage import Warehouse
    from StockInvestmentTool.warehouse.ths_industry import import_ths_industry_membership
    warehouse = Warehouse()
    warehouse.metadata.register_dataset("ths_industry_membership")
    result = import_ths_industry_membership(warehouse, snapshot_date=args.snapshot_date,
                                            csv_path=args.csv_path)
    print(result)


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        prog="StockInvestmentTool.warehouse",
        description="全量数据仓库：离线采集 / 因子计算 / 全市场扫描 / 在线快照",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="首建：代码清单 + 全量日线")
    p_init.add_argument("--years", type=int, default=3, help="已废弃，仅保留参数兼容")
    p_init.add_argument("--start", required=False, help="起始日期 YYYY-MM-DD")
    p_init.add_argument("--end", required=False, help="结束日期 YYYY-MM-DD")
    p_init.add_argument("--include-index", action="store_true", help="含指数")
    p_init.add_argument("--max-symbols", type=int, default=None, help="限定标的数(测试)")

    p_sync = sub.add_parser("sync", help="增量同步日线")
    p_sync.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD（必须显式传入）")
    p_sync.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD（必须显式传入）")
    p_sync.add_argument("--include-index", action="store_true", help="含指数")
    p_sync.add_argument("--max-symbols", type=int, default=None)
    p_sync.add_argument("--flush-every", type=int, default=1000,
                        help="每 N 个标的落盘一次，控制内存峰值(2C2G)")
    p_sync.add_argument("--source", default="baostock",
                        choices=["baostock", "tencent"],
                        help="数据源: baostock(默认)/tencent(腾讯,不封IP)")
    p_sync.add_argument("--target", default=None,
                        help="仅允许 raw:<source>；daily 直写已下线")

    p_scan = sub.add_parser("scan", help="全市场指标扫描（indicators 分区）")
    p_scan.add_argument("--start", required=True)
    p_scan.add_argument("--end", required=True)
    p_scan.add_argument("--where", default="", help="SQL 过滤条件")
    p_scan.add_argument("--order-by", default="vol_ratio DESC")
    p_scan.add_argument("--limit", type=int, default=50)
    p_scan.add_argument("--columns", default="code,date,close,vol_ratio,ret_5d")

    p_online = sub.add_parser("online", help="观察池在线快照")
    p_online.add_argument("--codes", default="", help="逗号分隔代码，空=用观察池")
    p_online.add_argument("--day", default=None, help="YYYY-MM-DD")

    p_public = sub.add_parser("export-public-daily", help="从 Published 日线导出静态公开 JSON")
    p_public.add_argument("--codes", default="", help="逗号分隔股票代码；空时使用 PUBLIC_DAILY_EXPORT_CODES 或默认列表")
    p_public.set_defaults(func=cmd_export_public_daily)

    p_ths = sub.add_parser("ths-industry-import", help="导入固定 GitHub 同花顺行业成员快照")
    p_ths.add_argument("--snapshot-date", required=True, help="YYYY-MM-DD")
    p_ths.add_argument("--csv-path", type=__import__("pathlib").Path, default=None,
                       help="可选本地 CSV；未指定时只读取固定 commit")
    p_ths.set_defaults(func=cmd_ths_industry_import)

    p_reset = sub.add_parser("reset", help="清空仓库数据(破坏性)")
    p_reset.add_argument("--kinds", default="daily,factor,online", help="daily/factor/online")
    p_reset.add_argument("--force", action="store_true", help="跳过确认")

    p_process = sub.add_parser("process", help="贴源层→加工层(daily 完整宽表)")
    p_process.add_argument("--months", default="", help="逗号分隔月份，空=全部")

    p_backfill = sub.add_parser("backfill", help="单点回补历史PE/PB(东财)")
    p_backfill.add_argument("--codes", required=True, help="逗号分隔代码 sh600900")
    p_backfill.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_backfill.add_argument("--end", required=True, help="YYYY-MM-DD")

    p_fund = sub.add_parser("fundamentals", help="采集财务史（行业采集已迁移）")
    p_fund.add_argument("--kind", choices=["industry", "financial", "all"],
                        default="all", help="采集类型")
    p_fund.add_argument("--max-symbols", type=int, default=None)
    p_fund.add_argument("--refresh", action="store_true", help="行业强制重采(默认跳过已采)")

    sub.add_parser("status", help="仓库状态")

    p_ind = sub.add_parser("industry", help="低频采集行业数据（默认不执行全市场）")
    p_ind.add_argument("kind", choices=["membership", "daily"])
    p_ind.add_argument("--max-symbols", type=int, default=None)
    p_ind.add_argument("--snapshot-date", default=None)
    p_ind.add_argument("--start", default=None)
    p_ind.add_argument("--end", default=None)
    p_ind.add_argument("--refresh", action="store_true")
    p_ind.add_argument("--interval", type=float, default=1.0)
    p_ind.add_argument("--retries", type=int, default=3)
    p_ind.add_argument("--backoff", type=float, default=2.0)

    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    handlers = {"init": cmd_init, "sync": cmd_sync,
                "scan": cmd_scan, "online": cmd_online, "status": cmd_status,
                "reset": cmd_reset, "process": cmd_process, "backfill": cmd_backfill,
                "fundamentals": cmd_fundamentals, "industry": cmd_industry,
                 "ths-industry-import": cmd_ths_industry_import,
                 "export-public-daily": cmd_export_public_daily}
    try:
        handlers[args.cmd](args)
    except Exception as e:
        logger.exception("命令 %s 执行失败", args.cmd)
        print(f"❌ 失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
