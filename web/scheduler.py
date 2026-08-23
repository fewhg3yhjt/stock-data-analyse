# -*- coding: utf-8 -*-
"""每日自动任务 — APScheduler 定时执行

时间通过环境变量配置：DAILY_RUN_TIME（默认 15:35，Asia/Shanghai）。
设 DISABLE_SCHEDULER=1 可关闭。

每日任务内容（收盘后自动流转，生成相关指标）：
  ① 持仓刷新：现价/止盈止损点位/建议 自动更新
  ② 持仓 → 自选 同步（去重）
  ③ 观察池重算：候选（自选+资金流）指标更新
  ④ 生成晨报
  ④' 数据仓库离线采集（可选，WAREHOUSE_DAILY_SYNC=1 开启）
  ⑤ notifier 推送（价格+资金流+盘后+持仓指令）

手动触发：web 管理页「立即运行每日任务」按钮 或 POST /api/daily/run。
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_RUN_TIME = "15:35"
TZ = "Asia/Shanghai"


def _parse_time(spec: str) -> tuple[int, int]:
    """'15:35' → (15, 35)"""
    spec = (spec or DEFAULT_RUN_TIME).strip()
    try:
        hh, mm = spec.split(":")
        return int(hh), int(mm)
    except Exception:
        logger.warning("DAILY_RUN_TIME 格式错误(%s)，使用默认 %s", spec, DEFAULT_RUN_TIME)
        return 15, 35


def run_daily_tasks() -> dict:
    """每日自动任务主体。"""
    logger.info("=== 每日自动任务开始 ===")
    from StockInvestmentTool.portfolio.manager import PortfolioManager

    mgr = PortfolioManager()
    results: dict = {}

    # ① 持仓刷新（现价/点位/建议）
    try:
        r = mgr.refresh_all()
        results["holdings"] = len(r) if isinstance(r, list) else r
    except Exception as e:
        logger.error("持仓刷新失败: %s", e)
        results["holdings"] = f"error: {e}"

    # ② 持仓 → 自选同步
    try:
        results["watchlist_sync"] = mgr.sync_holdings_to_watchlist()
    except Exception as e:
        logger.error("自选同步失败: %s", e)

    # ③ 观察池重算（候选指标更新，写缓存）
    try:
        from StockInvestmentTool.portfolio.dashboard import DashboardService
        rows = DashboardService(mgr).observe_pool(use_cache=False)
        results["observe"] = len(rows)
    except Exception as e:
        logger.error("观察池重算失败: %s", e)

    # ④ 生成晨报
    try:
        from StockInvestmentTool.portfolio.reporter import MorningReporter
        results["report"] = MorningReporter(mgr).generate(refresh=True)
    except Exception as e:
        logger.error("晨报生成失败: %s", e)

    # ④' 数据仓库离线采集（可选，默认关闭）
    # 用 WAREHOUSE_DAILY_SYNC=1 开启。开盘期间请勿开启（会在盘中拉全量）。
    if os.getenv("WAREHOUSE_DAILY_SYNC") == "1":
        try:
            results["warehouse"] = run_warehouse_daily()
        except Exception as e:
            logger.error("数据仓库采集失败: %s", e)
            results["warehouse"] = f"error: {e}"

    # ⑤ 消息推送
    try:
        from StockInvestmentTool.notifier.cli import main as notifier_main
        import io as _io
        import contextlib
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            notifier_main(["--all"])
        results["notify"] = "sent"
    except Exception as e:
        logger.error("通知推送失败: %s", e)

    logger.info("=== 每日自动任务完成: %s ===", results)
    return results


def run_warehouse_daily() -> dict:
    """数据仓库每日离线采集：增量日线 → 因子计算 → 新股PE/PB回补。

    由 WAREHOUSE_DAILY_SYNC=1 开启（见 run_daily_tasks ④'）。
    历史深度取 WAREHOUSE_YEARS（默认3年），增量只补缺失日期（方案B）。
    """
    logger.info("=== 数据仓库离线采集开始 ===")
    import os as _os
    years = int(_os.getenv("WAREHOUSE_YEARS", "3"))
    from StockInvestmentTool.warehouse.collector import MarketCollector
    from StockInvestmentTool.warehouse.factors import FactorEngine

    result = {}
    c = MarketCollector()
    start_date = (datetime.now() - timedelta(days=years * 365)).strftime("%Y-%m-%d")
    sync_res = c.sync_daily(
        start_date=start_date,
        include_etf=True,
        include_index=False,
        source="tencent",
    )
    result["sync"] = sync_res

    # 因子计算（增量后全量重算因子宽表）
    try:
        factor_res = FactorEngine().build_factors()
        result["factors"] = factor_res
    except Exception as e:
        logger.error("因子计算失败: %s", e)
        result["factors"] = f"error: {e}"

    # 新股 PE/PB 回补（仅补刚新增/缺失的股票估值）
    try:
        from StockInvestmentTool.warehouse.backfill import ValuationBackfill
        from StockInvestmentTool.warehouse.storage import Warehouse

        w = Warehouse()
        df = w.read_daily(w.available_months("daily")[-1])
        if df is not None and not df.empty:
            codes = sorted([c for c in df["code"].unique()
                            if c.startswith(("sh6", "sz0", "sz3"))])
            vb = ValuationBackfill(w)
            bf = vb.backfill_many(codes, start_date, datetime.now().strftime("%Y-%m-%d"),
                                  reprocess=True)
            result["backfill"] = bf
    except Exception as e:
        logger.error("PE/PB回补失败: %s", e)
        result["backfill"] = f"error: {e}"

    logger.info("=== 数据仓库离线采集完成: %s ===", result)
    return result


def init_scheduler(app) -> None:
    """创建并启动 APScheduler（单容器方案：web 进程内定时任务）。"""
    if os.getenv("DISABLE_SCHEDULER") == "1" or os.getenv("PYTEST_CURRENT_TEST"):
        logger.info("定时任务已跳过（DISABLE_SCHEDULER=1 或测试环境）")
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler 未安装，定时任务不可用")
        return

    hour, minute = _parse_time(os.getenv("DAILY_RUN_TIME", DEFAULT_RUN_TIME))
    scheduler = BackgroundScheduler(timezone=TZ)
    scheduler.add_job(
        run_daily_tasks, CronTrigger(hour=hour, minute=minute, timezone=TZ),
        id="daily_tasks", misfire_grace_time=3600, coalesce=True,
    )
    # 盘中观察池实时快照：每 10 分钟一次（仅交易时段内实际取值）
    # 用 WAREHOUSE_ONLINE_SNAPSHOT=1 开启（默认关闭，避免过度采集）
    if os.getenv("WAREHOUSE_ONLINE_SNAPSHOT") == "1":
        scheduler.add_job(
            run_online_snapshot_job, CronTrigger(minute="*/10", timezone=TZ),
            id="online_snapshot", misfire_grace_time=600, coalesce=True,
        )
        logger.info("盘中观察池快照已启动: 每 10 分钟")
    # 盘中持仓操作提醒：刷新持仓，仅推「有操作建议」的（止盈/止损/加仓）
    # 策略配置在 notifier/notify_settings.yaml（管理台可编辑）
    ns = _load_notify_settings()
    intraday = ns.get("intraday") or {}
    if intraday.get("enabled"):
        monitor_min = int(intraday.get("interval_minutes", 10))
        scheduler.add_job(
            run_actionable_monitor, CronTrigger(minute=f"*/{monitor_min}", timezone=TZ),
            id="actionable_monitor", misfire_grace_time=600, coalesce=True,
        )
        logger.info("盘中持仓操作提醒已启动: 每 %d 分钟", monitor_min)
    # 盘后全持仓汇总：每日定时发送全部持仓状态
    post_close = ns.get("post_close") or {}
    if post_close.get("enabled"):
        pc_time = post_close.get("time", "15:35")
        try:
            pc_hh, pc_mm = str(pc_time).split(":")
            pc_hh, pc_mm = int(pc_hh), int(pc_mm)
        except (ValueError, TypeError):
            pc_hh, pc_mm = 15, 35
        scheduler.add_job(
            run_post_close_summary, CronTrigger(hour=pc_hh, minute=pc_mm, timezone=TZ),
            id="post_close_summary", misfire_grace_time=3600, coalesce=True,
        )
        logger.info("盘后持仓汇总已启动: %02d:%02d", pc_hh, pc_mm)
    scheduler.start()
    app.extensions["scheduler"] = scheduler
    logger.info("每日定时任务已启动: %02d:%02d (%s)", hour, minute, TZ)


def _load_notify_settings() -> dict:
    """读取通知策略配置（notify_settings.yaml）。"""
    try:
        from StockInvestmentTool.portfolio.settings import load_notify_settings
        return load_notify_settings()
    except Exception:
        return {}


# ── 通知去重（避免盘中每10分钟重复推送同一建议）──────────────
_NOTIFY_STATE = os.path.join(os.environ.get("STOCK_OUTPUT_DIR", ""), "data", "notify_state.json") \
    if os.environ.get("STOCK_OUTPUT_DIR") else None
# 去重窗口（小时）：同一持仓同一建议类型在此窗口内不重复推送
_DEDUP_HOURS = 24


def _notify_state_path() -> str:
    """通知去重状态文件路径（output/data/notify_state.json）。"""
    global _NOTIFY_STATE
    if _NOTIFY_STATE:
        return _NOTIFY_STATE
    from StockInvestmentTool.config import Config
    _NOTIFY_STATE = str(Config.DATA_DIR / "notify_state.json")
    return _NOTIFY_STATE


def _load_notify_state() -> dict:
    path = _notify_state_path()
    try:
        import json
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_notify_state(state: dict):
    try:
        import json
        from pathlib import Path
        path = Path(_notify_state_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("通知状态保存失败: %s", e)


def _filter_unnotified(data: dict) -> dict:
    """过滤掉去重窗口内已推送过的操作建议。

    返回 {data: 过滤后的 war_room data, messages: 去重后的消息列表}
    """
    from datetime import datetime as _dt
    from StockInvestmentTool.notifier.notify import build_actionable_messages

    state = _load_notify_state()
    now = _dt.now()
    positions = data.get("positions") or []
    kept_positions = []
    for p in positions:
        adv = p.get("advice") or {}
        if not adv.get("is_actionable"):
            continue
        key = f"{p.get('id')}:{adv.get('advice_type')}"
        last = state.get(key)
        dedup = False
        if last:
            try:
                last_dt = _dt.fromisoformat(last)
                hours = (now - last_dt).total_seconds() / 3600
                if hours < _DEDUP_HOURS:
                    dedup = True
            except Exception:
                pass
        if not dedup:
            kept_positions.append(p)
            state[key] = now.isoformat(timespec="seconds")
    _save_notify_state(state)
    data = dict(data)
    data["positions"] = kept_positions
    msgs = build_actionable_messages(data)
    return {"data": data, "messages": msgs}


def run_post_close_summary():
    """盘后全持仓汇总：发送所有持仓的状态/建议/盈亏邮件。"""
    try:
        from StockInvestmentTool.portfolio.manager import PortfolioManager
        from StockInvestmentTool.portfolio.dashboard import DashboardService
        from StockInvestmentTool.notifier.notify import (
            NotifyRules, build_orders_messages, build_orders_html, send_all,
        )
        from StockInvestmentTool.notifier.channels import EmailSender

        mgr = PortfolioManager()
        mgr.refresh_all()
        data = DashboardService(mgr).war_room()
        messages = build_orders_messages(data)
        if not messages:
            logger.info("盘后汇总: 无持仓")
            return

        rules = NotifyRules.from_yaml()
        if rules.channel in ("email", "mail", "smtp"):
            html = build_orders_html(data)
            sender = EmailSender()
            sender.send(html, subject="📊 盘后持仓汇总", is_html=True)
            logger.info("盘后持仓汇总已推送邮件")
        else:
            webhook = rules.webhook_url()
            send_all(rules.channel, webhook, messages)
            logger.info("盘后持仓汇总已推送 %d 条", len(messages))
    except Exception as e:
        logger.error("盘后持仓汇总异常: %s", e)


def run_actionable_monitor():
    """盘中持仓操作提醒：刷新所有持仓，仅推送「有操作建议」的持仓。

    触发条件: advisor 给出 buy_more/partial_sell/sell_all/adjust_stop
    （右侧止盈/硬止损/技术止损/加仓/调止损），hold 不推。
    """
    try:
        from StockInvestmentTool.portfolio.manager import PortfolioManager
        from StockInvestmentTool.portfolio.dashboard import DashboardService
        from StockInvestmentTool.notifier.notify import (
            NotifyRules, build_actionable_messages, send_all,
        )

        mgr = PortfolioManager()
        # 刷新持仓（现价/点位/建议）
        mgr.refresh_all()
        data = DashboardService(mgr).war_room()
        messages = build_actionable_messages(data)
        if not messages:
            logger.info("持仓操作提醒: 无触发（全部 hold 或无持仓）")
            return

        # 去重：同一持仓同一建议类型短期内(默认24h)不重复推送
        dedup = _filter_unnotified(data)
        if not dedup["messages"]:
            logger.info("持仓操作提醒: 均已在去重期内推送过，跳过")
            return
        data = dedup["data"]

        rules = NotifyRules.from_yaml()
        try:
            webhook = rules.webhook_url()
        except RuntimeError as e:
            logger.error("操作提醒推送配置错误: %s", e)
            return

        # 邮件渠道：正文用 HTML 摘要卡片 + 外部URL快照图；webhook 渠道用纯文本
        if rules.channel in ("email", "mail", "smtp"):
            from StockInvestmentTool.notifier.notify import build_actionable_html
            from StockInvestmentTool.notifier.channels import EmailSender
            html_body = build_actionable_html(dedup["data"])
            # 外部 URL 快照图（避开 CID 内嵌触发 QQ 550 过滤）
            urls_2d = _build_snapshot_images(dedup["data"]) or []
            img_tags = "".join(
                f'<br><img src="{u}" style="max-width:640px;border-radius:8px;">'
                for group in urls_2d for u in group
            )
            sender = EmailSender()
            sender.send(html_body + img_tags, subject="🔔 持仓操作提醒",
                        images=None, is_html=True)
            logger.info("持仓操作提醒已推送邮件（%d 只有操作建议）",
                        len(dedup["messages"]))
        else:
            images = _build_snapshot_images(dedup["data"])
            sent = send_all(rules.channel, webhook, dedup["messages"], images=images)
            logger.info("持仓操作提醒已推送 %d 条（%d 只有操作建议）",
                        sent, len(dedup["messages"]))
    except Exception as e:
        logger.error("持仓操作提醒异常: %s", e)


def _build_snapshot_images(data: dict) -> Optional[list[list[str]]]:
    """为有操作建议的持仓生成收益快照图，返回与 messages 对齐的图片列表。

    每只股票一张收益图（存到 CHART_DIR 供外部 URL 访问），
    返回外部 URL 列表（邮件用 <img src> 引用，避开 CID 内嵌触发邮件过滤）。
    """
    channel = os.getenv("NOTIFY_CHANNEL", "")
    if channel in ("feishu", "wecom", "lark"):
        return None
    try:
        from StockInvestmentTool.analysis.returns import build_snapshot_chart
        from StockInvestmentTool.portfolio.monitor import PriceMonitor
        from StockInvestmentTool.config import Config
        from datetime import datetime as _dt

        out_dir = Config.CHART_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        positions = data.get("positions") or []
        urls_all = []
        monitor = PriceMonitor()
        for p in positions:
            adv = p.get("advice") or {}
            if not (adv.get("advice_type") and adv.get("is_actionable")):
                urls_all.append([])
                continue
            code = p.get("stock_code") or ""
            try:
                kline, _ = monitor.fetch_context_data(code)
                if kline is not None and not kline.empty:
                    name = p.get("stock_name") or code
                    # 存到 CHART_DIR，文件名带 notify_ 前缀（可被 /charts/ 访问）
                    fname = f"notify_{_dt.now():%Y%m%d%H%M%S}_{code.replace('.','_')}.png"
                    path = build_snapshot_chart(kline, name, code,
                                                out_dir=out_dir,
                                                filename=fname)
                    if path:
                        url = f"https://stock.easyconnect.ltd/charts/{os.path.basename(path)}"
                        urls_all.append([url])
                    else:
                        urls_all.append([])
                else:
                    urls_all.append([])
            except Exception as e:
                logger.warning("快照图生成失败 %s: %s", code, e)
                urls_all.append([])
        return urls_all
    except Exception as e:
        logger.warning("快照图生成整体失败: %s", e)
        return None


def run_online_snapshot_job():
    """盘中观察池快照定时任务主体（10 分钟一次）。"""
    from StockInvestmentTool.warehouse.online import run_online_snapshot
    try:
        result = run_online_snapshot()
        if result.get("ok"):
            logger.info("盘中快照已采集: %s", result.get("path"))
        else:
            logger.warning("盘中快照失败: %s", result.get("error"))
    except Exception as e:
        logger.error("盘中快照任务异常: %s", e)


def scheduler_status(app) -> dict:
    """定时任务状态（管理页展示）。"""
    sched = app.extensions.get("scheduler")
    if sched is None:
        return {"enabled": False}
    jobs = sched.get_jobs()
    next_run = str(jobs[0].next_run_time)[:16] if jobs else None
    return {"enabled": True, "next_run": next_run,
            "spec": os.getenv("DAILY_RUN_TIME", DEFAULT_RUN_TIME)}
