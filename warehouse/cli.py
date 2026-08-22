# -*- coding: utf-8 -*-
"""全量数据仓库 CLI — 离线全量采集 / 因子计算 / 全市场扫描 / 在线快照

用法:
    python -m StockInvestmentTool.warehouse init [--years 3] [--include-index]
    python -m StockInvestmentTool.warehouse sync    [--start YYYY-MM-DD] [--max-symbols N]
    python -m StockInvestmentTool.warehouse factors [--max-symbols N]
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
    """首建：同步全市场代码清单 + 全量日线（默认近3年）。"""
    from StockInvestmentTool.warehouse.collector import MarketCollector
    c = MarketCollector()
    n = c.sync_instruments(include_etf=True, include_index=args.include_index)
    print(f"✅ 标的清单已入库: {n} 条")
    start = (datetime.now() - timedelta(days=args.years * 365)).strftime("%Y-%m-%d")
    res = c.sync_daily(start_date=start, include_etf=True,
                       include_index=args.include_index, max_symbols=args.max_symbols)
    print(f"✅ 日线同步: +{res['added_rows']} 行, 失败 {len(res['failed'])}")
    if res["failed"]:
        print(f"   失败代码(前10): {res['failed'][:10]}")


def cmd_sync(args):
    """增量同步日线（收盘后每日跑）。"""
    from StockInvestmentTool.warehouse.collector import MarketCollector
    c = MarketCollector()
    res = c.sync_daily(start_date=args.start, include_etf=True,
                       include_index=args.include_index, max_symbols=args.max_symbols,
                       flush_every=args.flush_every, source=args.source)
    print(f"✅ 日线增量: +{res['added_rows']} 行, 失败 {len(res['failed'])}, 耗时 {res['elapsed_sec']}s")


def cmd_factors(args):
    """计算全市场因子宽表。"""
    from StockInvestmentTool.warehouse.factors import FactorEngine
    fe = FactorEngine()
    res = fe.build_factors(max_symbols=args.max_symbols)
    print(f"✅ 因子计算: {res['symbols']} 标的, {res['months']} 个月, 耗时 {res['elapsed_sec']}s")


def cmd_scan(args):
    """全市场因子扫描。"""
    from StockInvestmentTool.warehouse.scanner import MarketScanner
    s = MarketScanner()
    hits = s.scan(start=args.start, end=args.end,
                  where=args.where or "", order_by=args.order_by,
                  limit=args.limit, columns=args.columns)
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
    factor = w.available_months("factor")
    print(f"日线分区: {len(daily)} 个月  {daily[:3]}{'...' if len(daily)>3 else ''}")
    print(f"因子分区: {len(factor)} 个月  {factor[:3]}{'...' if len(factor)>3 else ''}")
    if daily:
        last = w.read_daily(daily[-1])
        if last is not None and len(last):
            print(f"最新数据: {last['date'].max().date()} ({len(last)} 行)")
    online = sorted(p.name for d in w.online_dir.iterdir() if d.is_dir()
                    for p in w.online_dir.joinpath(d.name).glob("*.csv"))
    print(f"在线快照: {len(online)} 份")
    print("=" * 56)


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        prog="StockInvestmentTool.warehouse",
        description="全量数据仓库：离线采集 / 因子计算 / 全市场扫描 / 在线快照",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="首建：代码清单 + 全量日线")
    p_init.add_argument("--years", type=int, default=3, help="历史深度年数(默认3)")
    p_init.add_argument("--include-index", action="store_true", help="含指数")
    p_init.add_argument("--max-symbols", type=int, default=None, help="限定标的数(测试)")

    p_sync = sub.add_parser("sync", help="增量同步日线")
    p_sync.add_argument("--start", default=None, help="起始日期(默认近3年)")
    p_sync.add_argument("--include-index", action="store_true", help="含指数")
    p_sync.add_argument("--max-symbols", type=int, default=None)
    p_sync.add_argument("--flush-every", type=int, default=1000,
                        help="每 N 个标的落盘一次，控制内存峰值(2C2G)")
    p_sync.add_argument("--source", default="baostock",
                        choices=["baostock", "tencent"],
                        help="数据源: baostock(默认)/tencent(腾讯,不封IP)")

    p_factors = sub.add_parser("factors", help="计算因子宽表")
    p_factors.add_argument("--max-symbols", type=int, default=None)

    p_scan = sub.add_parser("scan", help="全市场因子扫描")
    p_scan.add_argument("--start", required=True)
    p_scan.add_argument("--end", required=True)
    p_scan.add_argument("--where", default="", help="SQL 过滤条件")
    p_scan.add_argument("--order-by", default="vol_ratio DESC")
    p_scan.add_argument("--limit", type=int, default=50)
    p_scan.add_argument("--columns", default="code,date,close,vol_ratio,ret_5d")

    p_online = sub.add_parser("online", help="观察池在线快照")
    p_online.add_argument("--codes", default="", help="逗号分隔代码，空=用观察池")
    p_online.add_argument("--day", default=None, help="YYYY-MM-DD")

    p_reset = sub.add_parser("reset", help="清空仓库数据(破坏性)")
    p_reset.add_argument("--kinds", default="daily,factor,online", help="daily/factor/online")
    p_reset.add_argument("--force", action="store_true", help="跳过确认")

    sub.add_parser("status", help="仓库状态")

    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    handlers = {"init": cmd_init, "sync": cmd_sync, "factors": cmd_factors,
                "scan": cmd_scan, "online": cmd_online, "status": cmd_status,
                "reset": cmd_reset}
    try:
        handlers[args.cmd](args)
    except Exception as e:
        logger.exception("命令 %s 执行失败", args.cmd)
        print(f"❌ 失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()