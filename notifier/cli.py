# -*- coding: utf-8 -*-
"""股票消息提醒命令行入口

用法:
    python -m StockInvestmentTool.notifier --test            # 发一条测试消息验证 webhook
    python -m StockInvestmentTool.notifier --price           # 检查自选股价格阈值
    python -m StockInvestmentTool.notifier --fundflow        # 资金流信号提醒
    python -m StockInvestmentTool.notifier --daily           # 每日盘后汇总
    python -m StockInvestmentTool.notifier --all             # price + fundflow + daily

Webhook 配置在 StockInvestmentTool/.env:
    NOTIFY_CHANNEL=feishu            # feishu | wecom
    FEISHU_WEBHOOK_URL=...
    WECOM_WEBHOOK_URL=...

Windows 计划任务示例（每天 15:35 盘后）:
    python -m StockInvestmentTool.notifier --all
退出码: 0=成功, 1=推送失败。
"""

import argparse
import logging
import sys

from StockInvestmentTool.notifier.notify import (
    NotifyRules,
    build_daily_messages,
    build_fundflow_messages,
    build_orders_messages,
    build_price_messages,
    send_all,
)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m StockInvestmentTool.notifier",
        description="股票消息提醒：价格阈值 / 资金流信号 / 每日盘后汇总（企微或飞书群机器人）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--rules", default=None, help="规则 YAML 路径（缺省用包内 rules.yaml）")
    p.add_argument("--test", action="store_true", help="发送一条测试消息")
    p.add_argument("--price", action="store_true", help="检查自选股价格阈值并推送")
    p.add_argument("--fundflow", action="store_true", help="推送资金流信号")
    p.add_argument("--daily", action="store_true", help="推送每日资金流汇总")
    p.add_argument("--orders", action="store_true", help="推送作战仓今日持仓指令")
    p.add_argument("--all", action="store_true", help="price + fundflow + daily + orders")
    p.add_argument("--dry-run", action="store_true", help="只打印消息不真正推送")
    p.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    rules = NotifyRules.from_yaml(args.rules)

    try:
        webhook = None if args.dry_run else rules.webhook_url()
    except RuntimeError as e:
        print(f"[配置错误] {e}", file=sys.stderr)
        return 1

    messages: list[str] = []

    if args.test:
        messages.append(f"✅ 股票提醒测试消息（渠道 {rules.channel}，{__file__}）")

    if args.price or args.all:
        from StockInvestmentTool.screener.sources import tencent_quotes

        codes = [w["code"] for w in rules.watchlist if w.get("code")]
        if codes:
            quotes = tencent_quotes(codes).to_dict("records")
            messages.extend(build_price_messages(rules, quotes))

    if args.fundflow or args.all:
        messages.extend(_fundflow_messages(rules))

    if args.daily or args.all:
        messages.extend(_daily_messages(rules))

    if args.orders or args.all:
        from StockInvestmentTool.portfolio.dashboard import DashboardService

        data = DashboardService().war_room()
        messages.extend(build_orders_messages(data))

    if not messages:
        print("没有待推送的消息（无触发的价格阈值/信号，或未开启对应功能）")
        return 0

    if args.dry_run:
        print(f"===== dry-run: 共 {len(messages)} 条消息，渠道 {rules.channel} =====")
        for i, m in enumerate(messages, 1):
            print(f"\n----- 消息 {i} -----\n{m}")
        return 0

    sent = send_all(rules.channel, webhook, messages)
    print(f"已推送 {sent} 条")
    return 0


def _fundflow_messages(rules: NotifyRules) -> list[str]:
    """拉取同花顺资金流并构造信号提醒。"""
    from StockInvestmentTool.fundflow import analysis, sources

    stk_now = sources.fetch_stock("now")
    overview = analysis.market_overview(stk_now)
    stk_3d = sources.fetch_stock("3d")
    res = analysis.stock_analysis(stk_now, stk_3d, top=15)
    ind_now = sources.fetch_sector("industry", "now")
    ind_3d = sources.fetch_sector("industry", "3d")
    ind = analysis.merge_trend(ind_now, ind_3d, on="name")
    ind = ind.sort_values("net", ascending=False)
    sustained = res["持续流入榜"]
    divergent = res["价涨钱走(背离)榜"]
    turn = ind[ind["trend"] == "转为流入"].sort_values("net", ascending=False)
    return build_fundflow_messages(rules, overview, sustained, divergent, turn)


def _daily_messages(rules: NotifyRules) -> list[str]:
    """每日盘后汇总（资金流概况 + 行业榜）。"""
    from StockInvestmentTool.fundflow import analysis, sources

    stk_now = sources.fetch_stock("now")
    overview = analysis.market_overview(stk_now)
    ind_now = sources.fetch_sector("industry", "now").sort_values("net", ascending=False)
    ind_3d = sources.fetch_sector("industry", "3d").sort_values("net", ascending=False)
    return build_daily_messages(rules, overview, ind_now, ind_3d)


if __name__ == "__main__":
    sys.exit(main())
