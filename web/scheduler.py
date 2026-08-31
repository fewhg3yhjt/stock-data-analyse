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
import threading
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_RUN_TIME = "15:35"
TZ = "Asia/Shanghai"


def _market_session_minute_trigger():
    """Return one-minute triggers limited to A-share trading sessions."""
    from apscheduler.triggers.cron import CronTrigger

    # 09:30-11:30 and 13:00-15:00; the job itself tolerates holidays.
    return [
        CronTrigger(day_of_week="mon-fri", hour=9, minute="30-59", timezone=TZ),
        CronTrigger(day_of_week="mon-fri", hour=10, minute="*", timezone=TZ),
        CronTrigger(day_of_week="mon-fri", hour=11, minute="0-30", timezone=TZ),
        CronTrigger(day_of_week="mon-fri", hour=13, minute="*", timezone=TZ),
        CronTrigger(day_of_week="mon-fri", hour=14, minute="*", timezone=TZ),
    ]


def _market_session_intraday_trigger(interval: int):
    """Return an intraday trigger limited to A-share trading sessions."""
    from apscheduler.triggers.cron import CronTrigger

    minute = f"*/{interval}"
    return [
        CronTrigger(day_of_week="mon-fri", hour=9, minute="30,40,50", timezone=TZ),
        CronTrigger(day_of_week="mon-fri", hour=10, minute=minute, timezone=TZ),
        CronTrigger(day_of_week="mon-fri", hour=11, minute="0,10,20,30", timezone=TZ),
        CronTrigger(day_of_week="mon-fri", hour=13, minute=minute, timezone=TZ),
        CronTrigger(day_of_week="mon-fri", hour=14, minute=minute, timezone=TZ),
    ]


def _parse_time(spec: str) -> tuple[int, int]:
    """'15:35' → (15, 35)"""
    spec = (spec or DEFAULT_RUN_TIME).strip()
    try:
        hh, mm = spec.split(":")
        return int(hh), int(mm)
    except Exception:
        logger.warning("DAILY_RUN_TIME 格式错误(%s)，使用默认 %s", spec, DEFAULT_RUN_TIME)
        return 15, 35


def run_daily_tasks(run_id: int | None = None) -> dict:
    """每日自动任务主体。"""
    logger.info("=== 每日自动任务开始 ===")
    from StockInvestmentTool.ops.job_runs import JobRunStore
    owns_run = run_id is None
    run_id = run_id or JobRunStore().start("daily_tasks")
    from StockInvestmentTool.portfolio.manager import PortfolioManager

    mgr = PortfolioManager()
    results: dict = {}
    failures = []

    # ① 持仓刷新（现价/点位/建议）
    try:
        r = mgr.refresh_all()
        results["holdings"] = len(r) if isinstance(r, list) else r
    except Exception as e:
        logger.error("持仓刷新失败: %s", e)
        results["holdings"] = f"error: {e}"
        failures.append("holdings")

    # ② 持仓 → 自选同步
    try:
        results["watchlist_sync"] = mgr.sync_holdings_to_watchlist()
    except Exception as e:
        logger.error("自选同步失败: %s", e)
        failures.append("watchlist_sync")

    # ③ 观察池重算（候选指标更新，写缓存）
    try:
        from StockInvestmentTool.portfolio.dashboard import DashboardService
        rows = DashboardService(mgr).observe_pool(use_cache=False)
        results["observe"] = len(rows)
    except Exception as e:
        logger.error("观察池重算失败: %s", e)
        failures.append("observe")

    # ④ 生成晨报
    try:
        from StockInvestmentTool.portfolio.reporter import MorningReporter
        results["report"] = MorningReporter(mgr).generate(refresh=True)
    except Exception as e:
        logger.error("晨报生成失败: %s", e)
        failures.append("report")

    # 数据仓库由独立 daily_sync 任务负责，避免被持仓刷新/晨报阻塞或重复执行。

    # ⑤ 消息推送
    try:
        results["notify"] = run_daily_digest(mgr)
    except Exception as e:
        logger.error("通知推送失败: %s", e)
        failures.append("notify")

    results["failures"] = failures
    if owns_run or run_id:
        JobRunStore().finish(run_id, "failed" if failures else "success", results)
    logger.info("=== 每日自动任务完成: %s ===", results)
    return results


def run_daily_digest(mgr=None) -> dict:
    """Build one post-close digest from price/fundflow/daily/order topics."""
    from StockInvestmentTool.notifier.core import (
        MessageAggregator, NotificationFragment, TOPIC_PRICE, TOPIC_FUNDFLOW,
        TOPIC_SUMMARY, TOPIC_ORDERS, PRIORITY_BATCH,
    )
    from StockInvestmentTool.notifier.notify import (
        NotifyRules, build_price_messages, build_fundflow_messages,
        build_daily_messages, build_orders_messages,
    )
    from StockInvestmentTool.screener.sources import tencent_quotes
    from StockInvestmentTool.fundflow import analysis, sources
    from StockInvestmentTool.portfolio.dashboard import DashboardService

    rules = NotifyRules.from_yaml()
    agg = MessageAggregator()
    codes = [w["code"] for w in rules.watchlist if w.get("code")]
    if codes:
        lines = build_price_messages(rules, tencent_quotes(codes).to_dict("records"))
        agg.add(NotificationFragment(TOPIC_PRICE, "💰 自选价格提醒", lines, PRIORITY_BATCH))

    stk_now = sources.fetch_stock("now")
    overview = analysis.market_overview(stk_now)
    stk_3d = sources.fetch_stock("3d")
    stock_res = analysis.stock_analysis(stk_now, stk_3d, top=15)
    ind_now = sources.fetch_sector("industry", "now")
    ind_3d = sources.fetch_sector("industry", "3d")
    industries = analysis.merge_trend(ind_now, ind_3d, on="name")
    sustained = stock_res["持续流入榜"]
    divergent = stock_res["价涨钱走(背离)榜"]
    turn = industries[industries["trend"] == "转为流入"].sort_values("net", ascending=False)
    agg.add(NotificationFragment(TOPIC_FUNDFLOW, "🌊 资金流信号",
                                 build_fundflow_messages(rules, overview, sustained, divergent, turn), PRIORITY_BATCH))
    agg.add(NotificationFragment(TOPIC_SUMMARY, "📊 盘后市场汇总",
                                 build_daily_messages(rules, overview, ind_now, ind_3d), PRIORITY_BATCH))
    data = DashboardService(mgr or __import__("StockInvestmentTool.portfolio.manager", fromlist=["PortfolioManager"]).PortfolioManager()).war_room()
    agg.add(NotificationFragment(TOPIC_ORDERS, "⚔️ 今日持仓指令",
                                 build_orders_messages(data), PRIORITY_BATCH))
    digest = agg.digest(meta={"subject": f"股票盘后汇总 {datetime.now():%Y-%m-%d}"})
    if digest is None:
        return {"ok": True, "skipped": True}
    channel = rules.channel
    kwargs = {"subject": digest.meta["subject"]} if channel in ("email", "mail", "smtp") else {}
    return send_digest_with_outbox(digest, channel, **kwargs)


def run_warehouse_daily() -> dict:
    """数据仓库每日离线采集：增量日线 → 因子计算 → 新股PE/PB回补。

    由 WAREHOUSE_DAILY_SYNC=1 开启（见 run_daily_tasks ④'）。
    历史深度取 WAREHOUSE_YEARS（默认3年），增量只补缺失日期（方案B）。
    """
    logger.info("=== 数据仓库离线采集开始 ===")
    import os as _os
    years = int(_os.getenv("WAREHOUSE_YEARS", "3"))
    from StockInvestmentTool.warehouse.collector import MarketCollector

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

    # 指标批量生成（采集后自动重建 indicators/ 分区，下游消费最新指标）
    try:
        from StockInvestmentTool.warehouse.indicators_build import IndicatorsBuilder
        ind_res = IndicatorsBuilder().build_all()
        result["indicators"] = ind_res
    except Exception as e:
        logger.error("指标批量生成失败: %s", e)
        result["indicators"] = f"error: {e}"

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


def run_daily_data_pipeline(run_id: int | None = None) -> dict:
    """Run the warehouse chain independently from portfolio reporting."""
    from StockInvestmentTool.ops.job_runs import JobRunStore

    store = JobRunStore()
    run_date = datetime.now().strftime("%Y-%m-%d")
    owns_run = run_id is None
    run_id = run_id or store.start("daily_sync", display_name="日线增量同步",
                                   scheduled_at=os.getenv("DAILY_RUN_TIME", DEFAULT_RUN_TIME),
                                    input_dataset="source", output_dataset="daily")
    store.link_plan_run(run_date, "daily_sync", run_id)
    result = {}
    try:
        from StockInvestmentTool.warehouse.collector import MarketCollector
        from StockInvestmentTool.warehouse.indicators_build import IndicatorsBuilder

        def progress(processed, total, current, phase):
            store.update_progress(run_id, phase=phase,
                                  progress=round(processed / total * 100) if total else 0,
                                  processed=processed, total=total, current_item=current)

        years = int(os.getenv("WAREHOUSE_YEARS", "3"))
        start_date = (datetime.now() - timedelta(days=years * 365)).strftime("%Y-%m-%d")
        end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        child_statuses = []
        store.update_progress(run_id, phase="获取股票清单", progress=1)
        result["daily"] = MarketCollector().sync_daily(
            start_date=start_date, end_date=end_date, include_etf=True,
            include_index=False, source="tencent", progress_callback=progress,
            job_run_id=run_id,
        )
        if os.getenv("WAREHOUSE_PIPELINE_BUILD") == "1" and result["daily"].get("source_batch_id"):
            result["published_daily"] = publish_daily_batch(result["daily"]["source_batch_id"], run_id)
        if _has_enabled_auxiliary_tasks():
            result["auxiliary"] = run_auxiliary_data_pipeline(parent_run_id=run_id)
        daily_status = store.result_status(result["daily"])
        child_statuses.append(daily_status)
        if daily_status == "failed":
            raise RuntimeError("日线同步未产生有效产出")
        store.update_progress(run_id, phase="日线完成，开始重建指标", progress=33,
                              processed=1, total=2)
        indicator_id = store.start("rebuild_indicators", display_name="指标重建",
                                   input_dataset="daily", output_dataset="indicators",
                                   parent_run_id=run_id)
        store.link_plan_run(run_date, "rebuild_indicators", indicator_id)
        try:
            result["indicators"] = IndicatorsBuilder(allow_legacy=False).build_all(progress_callback=lambda p, t, c, s: (store.update_progress(indicator_id, phase=s, progress=round(p / t * 100) if t else 0, processed=p, total=t, current_item=c), store.update_progress(run_id, phase="重建指标", progress=33 + round((p / t * 100) * 0.67) if t else 33, processed=p, total=t, current_item=c)))
            indicator_status = store.result_status(result["indicators"])
            child_statuses.append(indicator_status)
            store.finish(indicator_id, indicator_status, result["indicators"])
        except Exception as exc:
            if store.get(indicator_id).get("status") == "running":
                store.finish(indicator_id, "failed", error=str(exc))
            child_statuses.append("failed")
            raise
        parent_status = ("failed" if "failed" in child_statuses else
                         "partial_success" if "partial_success" in child_statuses else
                         "skipped" if all(s == "skipped" for s in child_statuses) else "success")
        store.update_progress(run_id, phase="完成", progress=100)
        if store.get(run_id).get("status") == "running":
            store.finish(run_id, parent_status, result)
        return result
    except Exception as exc:
        if store.get(run_id) and store.get(run_id).get("status") == "running":
            store.finish(run_id, "failed", result=result, error=str(exc))
        raise


def run_auxiliary_data_pipeline(parent_run_id: int | None = None) -> dict:
    """Collect auxiliary datasets according to the Active Config."""
    from StockInvestmentTool.warehouse.fundamentals_collect import FundamentalsCollector
    from StockInvestmentTool.warehouse.storage import Warehouse
    from StockInvestmentTool.ops.task_center import TaskCenter, management_db_path
    from StockInvestmentTool.fundflow.capture import capture_money_flow

    warehouse = Warehouse()
    for name in ("industry", "fundamentals", "valuation_daily", "money_flow_daily"):
        warehouse.metadata.register_dataset(name)
    configured = TaskCenter(management_db_path()).active_configs()
    result = {"enabled_tasks": []}
    collector = FundamentalsCollector(warehouse=warehouse)
    if _task_schedule_enabled(configured, "industry_capture"):
        try:
            result["industry"] = collector.collect_industry()
        except Exception as exc:
            logger.error("行业采集失败: %s", exc)
            result["industry"] = {"failed": [str(exc)]}
        result["enabled_tasks"].append("industry_capture")
    if _task_schedule_enabled(configured, "fundamentals_capture"):
        try:
            result["fundamentals"] = collector.collect_fundamentals()
        except Exception as exc:
            logger.error("财务史采集失败: %s", exc)
            result["fundamentals"] = {"failed": 1, "error": str(exc)}
        result["enabled_tasks"].append("fundamentals_capture")
    if _task_schedule_enabled(configured, "valuation_capture"):
        try:
            from StockInvestmentTool.warehouse.backfill import ValuationBackfill
            daily = warehouse.read_daily(warehouse.available_months("daily")[-1])
            codes = sorted(c for c in daily["code"].unique()
                           if c.startswith(("sh6", "sz0", "sz3", "bj4", "bj8")))
            result["valuation"] = ValuationBackfill(warehouse).backfill_many(
                codes, (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"),
                (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"), reprocess=True)
        except Exception as exc:
            logger.error("估值采集失败: %s", exc)
            result["valuation"] = {"failed": 1, "error": str(exc)}
        result["enabled_tasks"].append("valuation_capture")
    if _task_schedule_enabled(configured, "money_flow_capture"):
        try:
            result["money_flow"] = capture_money_flow("stock", "now", warehouse=warehouse)
        except Exception as exc:
            logger.error("资金流采集失败: %s", exc)
            result["money_flow"] = {"ok": False, "error": str(exc)}
        result["enabled_tasks"].append("money_flow_capture")
    result["enabled"] = True
    return result


def _has_enabled_auxiliary_tasks() -> bool:
    """Whether any configured auxiliary capture task is enabled (Active Config)."""
    from StockInvestmentTool.ops.task_center import TaskCenter, management_db_path
    auxiliary_keys = {"industry_capture", "fundamentals_capture",
                      "valuation_capture", "money_flow_capture"}
    return any(
        item["task_key"] in auxiliary_keys
        and bool(item["enabled"])
        for item in TaskCenter(management_db_path()).active_configs().values()
    )


def _task_schedule_enabled(configured: dict[str, dict], task_key: str) -> bool:
    """Return whether an auxiliary task is enabled for scheduled execution."""
    item = configured.get(task_key) or {}
    return bool((item.get("schedule") or {}).get("enabled", False))


def publish_daily_batch(batch_id: str, job_run_id: int | None = None) -> dict:
    """Build, validate and publish one Tencent Raw Batch behind an opt-in flag."""
    from StockInvestmentTool.warehouse.daily_build import DailyBuilder
    from StockInvestmentTool.warehouse.pipeline_state import PipelineState
    from StockInvestmentTool.warehouse.publish import Publisher
    from StockInvestmentTool.warehouse.quality import check_stock_daily
    from StockInvestmentTool.warehouse.storage import Warehouse
    warehouse = Warehouse()
    with warehouse._conn() as conn:
        row = conn.execute("SELECT raw_path FROM source_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"Raw Batch 文件不存在: {batch_id}")
    raw_path = Path(row[0])
    partition = str(pd.Timestamp(pd.read_parquet(raw_path, columns=["date"])["date"].max()).strftime("%Y-%m"))
    build = DailyBuilder(warehouse).build_partition(partition, [("tencent", raw_path)])
    state = PipelineState(warehouse.meta_db_path)
    version = state.create_version(build, source_batches=[batch_id], input_versions={})
    quality = check_stock_daily(build["path"], expected_symbols=None)
    state.quality(version, status=quality["status"], checks=quality["checks"], publish_allowed=quality["publish_allowed"])
    if not quality["publish_allowed"]:
        return {"version_id": version, "quality": quality, "published": False, "job_run_id": job_run_id}
    published = Publisher(warehouse).publish(version)
    return {"version_id": version, "quality": quality, "published": published, "job_run_id": job_run_id}


def init_scheduler(app) -> None:
    """创建并启动 APScheduler（单容器方案：web 进程内定时任务）。"""
    startup_boundary = datetime.now()
    app.extensions["scheduler_state"] = {"running": False, "reason": "disabled_by_config" if os.getenv("DISABLE_SCHEDULER") == "1" else "not_initialized"}
    if os.getenv("DISABLE_SCHEDULER") == "1" or os.getenv("PYTEST_CURRENT_TEST"):
        logger.info("定时任务已跳过（DISABLE_SCHEDULER=1 或测试环境）")
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        app.extensions["scheduler_state"] = {"running": False, "reason": "dependency_missing"}
        logger.warning("APScheduler 未安装，定时任务不可用")
        return

    lock_file = None
    try:
        import fcntl
        from StockInvestmentTool.config import Config

        lock_path = Config.DATA_DIR / "scheduler.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_path, "a+", encoding="utf-8")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        app.extensions["scheduler_state"] = {"running": False, "reason": "lock_not_acquired"}
        logger.warning("已有其他进程持有 scheduler 锁，本进程不启动定时任务")
        if lock_file:
            lock_file.close()
        return

    hour, minute = _parse_time(os.getenv("DAILY_RUN_TIME", DEFAULT_RUN_TIME))
    scheduler = BackgroundScheduler(timezone=TZ)
    scheduler.add_job(
        run_daily_tasks, CronTrigger(hour=hour, minute=minute, timezone=TZ),
        id="daily_tasks", misfire_grace_time=3600, coalesce=True, max_instances=1,
    )
    _schedule_configured_data_tasks(scheduler)
    scheduler.add_job(
        process_notification_outbox, CronTrigger(minute="*/5", timezone=TZ),
        id="notification_outbox", misfire_grace_time=600, coalesce=True,
        max_instances=1,
    )
    # 分钟采集优先；只有分钟采集未启用时才启用低频在线快照兜底。
    minute_enabled = os.getenv("WAREHOUSE_MINUTE_SNAPSHOT") == "1"
    if os.getenv("WAREHOUSE_ONLINE_SNAPSHOT") == "1" and not minute_enabled:
        scheduler.add_job(
                run_online_snapshot_job, CronTrigger(minute="*/10", timezone=TZ),
                id="online_snapshot", misfire_grace_time=600, coalesce=True, max_instances=1,
        )
        logger.info("盘中观察池快照已启动: 每 10 分钟")
    if minute_enabled:
        for index, trigger in enumerate(_market_session_minute_trigger()):
            scheduler.add_job(
                run_minute_snapshot_job, trigger,
                id=f"minute_snapshot_{index}", misfire_grace_time=600, coalesce=True,
                max_instances=1,
            )
        logger.info("盘中分钟数据已启动: 每 1 分钟，观察池范围")
    elif os.getenv("WAREHOUSE_ONLINE_SNAPSHOT") == "1":
        logger.info("在线快照已忽略：分钟采集优先")

    # 通知触发器（FR-3.4 免重启：按触发器配置挂载，保存后重挂即可）
    _schedule_from_triggers(scheduler)

    _recover_stale_task_state()

    scheduler.start()
    app.extensions["scheduler"] = scheduler
    app.extensions["scheduler_state"] = {"running": True, "reason": "registered", "startup_at": startup_boundary.isoformat(timespec="seconds")}
    app.extensions["scheduler_lock"] = lock_file
    if lock_file is not None:
        import atexit
        atexit.register(lock_file.close)
    logger.info("每日定时任务已启动: %02d:%02d (%s)", hour, minute, TZ)


def _schedule_configured_data_tasks(scheduler) -> None:
    """Register enabled data tasks from the Active Config fact source.

    唯一事实源是 management.db 的 Active Config（sync_definitions 时从 YAML
    初始化，之后由任务中心保存/激活/启停控制）。YAML 的 schedule.enabled
    仅在初始同步时写入，不再作为运行期事实。
    """
    from apscheduler.triggers.cron import CronTrigger
    from StockInvestmentTool.ops.task_center import TaskCenter, management_db_path

    def run_task(task_key):
        from StockInvestmentTool.ops.task_execution import execute_task, execute_pipeline
        payload = {
            "trigger_type": "scheduled", "requested_by": "scheduler",
        }
        if task_key == "stock_daily_capture":
            configured_keys = TaskCenter(management_db_path()).active_configs()
            chain = [key for key in ("stock_daily_capture", "stock_daily_build", "stock_daily_quality",
                                     "stock_daily_publish", "indicators_build")
                     if (configured_keys.get(key) or {}).get("enabled")]
            return execute_pipeline(management_db_path(), chain or [task_key], payload)
        return execute_task(management_db_path(), task_key, payload)

    from StockInvestmentTool.ops.task_center import TaskCenter, management_db_path
    for item in TaskCenter(management_db_path()).active_configs().values():
        key = item["task_key"]
        schedule = item["schedule"] or {}
        if not item["enabled"]:
            continue
        frequency = schedule.get("frequency")
        spec = schedule.get("time")
        if frequency == "after_upstream" or not spec:
            continue
        hour, minute = _parse_time(spec)
        kwargs = {"day_of_week": "mon-fri"} if frequency == "trading_day" else {}
        if frequency in {"weekly", "quarterly"}:
            kwargs["day_of_week"] = "mon"
        if frequency == "monthly":
            kwargs["day"] = "1"
        scheduler.add_job(
            run_task, CronTrigger(hour=hour, minute=minute, timezone=TZ, **kwargs),
            id=f"task:{key}", args=[key], misfire_grace_time=21600,
            coalesce=True, max_instances=1, replace_existing=True,
        )
        logger.info("配置任务已注册: %s (%s %s)", key, frequency, spec)


def _recover_stale_task_state() -> int:
    """启动时回收遗留任务状态（进程重启后的残留在途状态）。

    回收 running JobRun、过期任务锁、running SourceBatch，并标记残留
    publishing 版本，避免重启后重复写入或永久卡死。
    """
    recovered = 0
    from datetime import datetime
    from StockInvestmentTool.ops.job_runs import JobRunStore
    store = JobRunStore()
    boundary = datetime.now() - timedelta(minutes=5)
    recovered += store.reclaim_data_running(before=boundary)
    recovered += store.recover_stale_locks()
    try:
        from StockInvestmentTool.ops.task_center import TaskCenter, management_db_path
        center = TaskCenter(management_db_path())
        recovered += center.recover_inflight_requests()
    except Exception as exc:
        logger.warning("任务中心遗留状态回收失败: %s", exc)
    try:
        from StockInvestmentTool.warehouse.pipeline_state import recover_inflight_publishing
        recovered += recover_inflight_publishing()
    except Exception as exc:
        logger.warning("publishing 版本回收失败: %s", exc)
    try:
        from StockInvestmentTool.web.app import _recover_analysis_tasks
        recovered += _recover_analysis_tasks()
    except Exception as exc:
        logger.warning("分析任务回收失败: %s", exc)
    if recovered:
        logger.info("启动回收遗留任务状态 %d 项", recovered)
    return recovered


def _reload_data_scheduler_jobs(app) -> None:
    """免重启：按 Active Config 重载数据任务（删除 task:* 再按 active 注册）。

    仅影响数据任务，不影响 Outbox、分钟快照和通知 Trigger。
    """
    sched = app.extensions.get("scheduler") if app else None
    if sched is None:
        return
    try:
        for job in sched.get_jobs():
            if job.id.startswith("task:"):
                job.remove()
        _schedule_configured_data_tasks(sched)
        logger.info("数据任务调度已按 Active Config 重载")
    except Exception as exc:
        logger.error("数据任务调度重载失败: %s", exc)


def _load_notify_settings() -> dict:
    """读取通知策略配置（notify_settings.yaml）。"""
    try:
        from StockInvestmentTool.portfolio.settings import load_notify_settings
        return load_notify_settings()
    except Exception:
        return {}


def _schedule_from_triggers(scheduler) -> None:
    """按触发器配置挂载定时任务（免重启生效 FR-3.4）。

    读取最新 triggers（notifier/notify_rules.yaml），每次保存后调用
    `_reload_scheduler_jobs` 重建作业，无需重启容器。
    """
    from StockInvestmentTool.notifier import triggers
    from apscheduler.triggers.cron import CronTrigger

    # 去掉旧的触发器作业（保留 daily_tasks / online_snapshot）
    for job in scheduler.get_jobs():
        if job.id in ("actionable_monitor", "post_close_summary"):
            job.remove()
        elif job.id.startswith("trigger_"):
            job.remove()

    for rule in triggers.enabled_triggers():
        sched = rule.get("schedule") or {}
        mode = sched.get("mode")
        rid = rule.get("id") or rule.get("name")
        channel = rule.get("channel", "feishu")
        instant = rule.get("priority") == "instant"
        try:
            if mode == "intraday":
                minutes = min(max(int(sched.get("interval_minutes", 10)), 5), 120)
                for index, trigger in enumerate(_market_session_intraday_trigger(minutes)):
                    scheduler.add_job(
                        lambda r=rule: run_trigger_rule(r), trigger,
                        id=f"trigger_{rid}_{index}", misfire_grace_time=600,
                        coalesce=True, max_instances=1, replace_existing=True,
                    )
                logger.info("通知触发器已挂载: %s（盘中每 %d 分钟，%s）", rid, minutes, channel)
            elif mode in ("post_close", "daily"):
                t = str(sched.get("time", "15:35"))
                try:
                    hh, mm = int(t.split(":")[0]), int(t.split(":")[1])
                except (ValueError, TypeError):
                    hh, mm = 15, 35
                scheduler.add_job(
                    lambda r=rule: run_trigger_rule(r),
                    CronTrigger(hour=hh, minute=mm, timezone=TZ),
                    id=f"trigger_{rid}", misfire_grace_time=3600, coalesce=True,
                    max_instances=1,
                    replace_existing=True,
                )
                logger.info("通知触发器已挂载: %s（%s %02d:%02d，%s）", rid, mode, hh, mm, channel)
        except Exception as e:
            logger.error("通知触发器挂载失败 %s: %s", rid, e)


def run_trigger_rule(rule: dict) -> dict:
    """执行一条触发器规则：按条件过滤持仓/信号，聚合发送。"""
    from StockInvestmentTool.notifier.core import (
        NotificationFragment, MessageAggregator, TOPIC_ORDERS,
        TOPIC_PRICE, TOPIC_SUMMARY, PRIORITY_BATCH, live_send_digest,
    )
    from StockInvestmentTool.portfolio.manager import PortfolioManager
    from StockInvestmentTool.portfolio.dashboard import DashboardService

    logger.info("执行通知触发器: %s", rule.get("name"))
    try:
        mgr = PortfolioManager()
        mgr.refresh_all()
        data = DashboardService(mgr).war_room()
    except Exception as e:
        logger.error("触发器数据获取失败: %s", e)
        return {"ok": False, "error": str(e)}

    # 组装片段（topic 分节）。每条条件先独立求值，再按 AND/OR 合并；
    # 不再只看 condition type，从而确保页面配置的参数真正影响通知。
    agg = MessageAggregator()
    conditions = [c for c in rule.get("conditions", []) if isinstance(c, dict)]
    condition_results = [_evaluate_trigger_condition(c, data) for c in conditions]
    logic = str(rule.get("logic", "AND")).upper()
    triggered = (all(condition_results) if logic == "AND" else any(condition_results)) if condition_results else True
    if not triggered:
        logger.info("触发器 %s 条件未满足: %s", rule.get("name"), condition_results)
        return {"ok": True, "skipped": True, "conditions": condition_results}

    # 操作建议类条件
    action_types = set()
    for condition in conditions:
        if condition.get("type") == "action":
            action_types.update((condition.get("params") or {}).get("advice_types") or [])
    if any(c.get("type") == "action" for c in conditions) or not conditions:
        orders_lines = _orders_lines(data, action_types or None)
        if orders_lines:
            agg.add(NotificationFragment(TOPIC_ORDERS, "⚔️ 今日持仓指令",
                                         orders_lines, PRIORITY_BATCH))

    # 价格阈值类（自选）
    if any(c.get("type") == "price_change" for c in conditions):
        price_lines = _watch_price_lines(
            next((c.get("params") or {} for c in conditions if c.get("type") == "price_change"), {})
        )
        if price_lines:
            agg.add(NotificationFragment(TOPIC_PRICE, "💰 自选价格提醒",
                                         price_lines, PRIORITY_BATCH))

    digest = agg.digest(meta={"subject": f"股票通知 {datetime.now():%Y-%m-%d}"})
    if digest is None:
        logger.info("触发器 %s 无触发内容，跳过", rule.get("name"))
        return {"ok": True, "skipped": True}

    # Intraday checks may run every few minutes, but ordinary alerts must not
    # turn that polling interval into the delivery frequency.
    cap_state = None
    if (rule.get("schedule") or {}).get("mode") == "intraday":
        cap_state = _intraday_trigger_quota(rule)
        if not cap_state["allowed"]:
            logger.info("触发器 %s 已达到每日通知上限 %d 次", rule.get("name"), cap_state["limit"])
            return {"ok": True, "skipped": True, "reason": "daily_limit",
                    "daily_count": cap_state["count"], "daily_limit": cap_state["limit"]}

    channel = rule.get("channel", "feishu")
    kwargs = {}
    if channel in ("email", "mail", "smtp"):
        kwargs["subject"] = digest.meta.get("subject", "股票通知")
        if rule.get("use_email_to"):
            kwargs["to"] = rule.get("use_email_to")
    result = send_digest_with_outbox(digest, channel, **kwargs)
    if result.get("ok") and cap_state:
        _commit_intraday_trigger_quota(cap_state)
    return result


def send_digest_with_outbox(digest, channel: str, **kwargs) -> dict:
    """Persist a Digest before delivery; mark sent only after success."""
    from StockInvestmentTool.notifier.outbox import NotificationOutbox

    payload = {"sections": digest.sections, "meta": digest.meta, "kwargs": kwargs}
    outbox = NotificationOutbox()
    item_id = outbox.enqueue(channel, payload)
    result = _deliver_outbox_item({"id": item_id, "channel": channel, "payload": payload, "attempts": 0})
    if result.get("ok"):
        outbox.mark_sent(item_id)
    return result


def _deliver_outbox_item(item: dict, worker_id: str | None = None) -> dict:
    from StockInvestmentTool.notifier.core import Digest, live_send_digest
    from StockInvestmentTool.notifier.outbox import NotificationOutbox

    payload = item["payload"]
    try:
        result = live_send_digest(
            Digest(sections=payload.get("sections", []), meta=payload.get("meta", {})),
            item["channel"], **(payload.get("kwargs") or {}),
        )
        if not result or result.get("ok", True) is False:
            raise RuntimeError(str(result))
        return result
    except Exception as exc:
        NotificationOutbox().mark_failed(item["id"], item.get("attempts", 0), str(exc), worker_id)
        return {"ok": False, "error": str(exc), "outbox_id": item["id"]}


def process_notification_outbox() -> dict:
    """Retry pending notifications after process/container restarts."""
    from StockInvestmentTool.notifier.outbox import NotificationOutbox

    worker_id = "scheduler"  # 当前无持久 Worker，仅 Scheduler 领取
    outbox = NotificationOutbox()
    due = outbox.claim_due(worker_id=worker_id, lease_seconds=300)
    if not due:
        return {"sent": 0, "failed": 0, "pending": outbox.counts().get("pending", 0),
                "dead": outbox.counts().get("dead", 0)}
    from StockInvestmentTool.ops.job_runs import JobRunStore
    store = JobRunStore()
    run_id = store.start("notification_outbox")
    sent = failed = 0
    for item in due:
        result = _deliver_outbox_item(item, worker_id)
        if result.get("ok"):
            outbox.mark_sent(item["id"], worker_id)
            sent += 1
        else:
            failed += 1
    counts = outbox.counts()
    result = {"sent": sent, "failed": failed, "pending": counts.get("pending", 0),
              "dead": counts.get("dead", 0)}
    store.finish(run_id, "success" if not failed else ("partial_success" if sent else "failed"), result)
    return result


def _evaluate_trigger_condition(condition: dict, data: dict) -> bool:
    """Evaluate one trigger condition against the current portfolio snapshot."""
    ctype = condition.get("type")
    params = condition.get("params") or {}
    if ctype == "action":
        wanted = set(params.get("advice_types") or [])
        if not wanted:
            return bool(data.get("positions"))
        return any((p.get("advice") or {}).get("advice_type") in wanted for p in data.get("positions") or [])
    if ctype == "price_change":
        return bool(_watch_price_lines(params))
    if ctype == "indicator":
        return _indicator_condition_matches(params, data)
    return False


def _indicator_condition_matches(params: dict, data: dict) -> bool:
    """Evaluate an indicator threshold/crossing for any open position."""
    from StockInvestmentTool.datasource.fetcher import StockDataFetcher
    from StockInvestmentTool.datasource.base import FallbackDataSource
    from StockInvestmentTool.indicators.context import IndicatorContext

    name = str(params.get("name") or "").strip()
    operator = str(params.get("operator") or "above").strip().lower()
    if not name:
        return False
    try:
        threshold = float(params.get("value"))
    except (TypeError, ValueError):
        return False
    source = FallbackDataSource()
    for position in data.get("positions") or []:
        code = position.get("stock_code") or position.get("code")
        if not code:
            continue
        frame = source.fetch_kline(code)
        if frame is None or len(frame) < 2:
            continue
        try:
            current = IndicatorContext(frame, row_index=len(frame) - 1).eval(name)
            previous = IndicatorContext(frame, row_index=len(frame) - 2).eval(name)
        except (ValueError, KeyError):
            continue
        if operator == "above" and current > threshold:
            return True
        if operator == "below" and current < threshold:
            return True
        if operator == "cross_above" and previous <= threshold < current:
            return True
        if operator == "cross_below" and previous >= threshold > current:
            return True
    return False


def _orders_lines(data: dict, advice_types: Optional[set[str]] = None) -> list[str]:
    """持仓指令 → 文本行（含操作建议/盈亏）。"""
    positions = data.get("positions") or []
    if not positions:
        return []
    detail_lines = []
    for p in positions:
        adv = p.get("advice") or {}
        if advice_types and adv.get("advice_type") not in advice_types:
            continue
        label = p.get("advice_label") or "—"
        reason = (adv.get("reason") or "")[:60]
        line = (f"{p.get('stock_name')}({p.get('stock_code')}): {label}"
                f" 现价{p.get('current_price')} 盈亏{p.get('unrealized_pnl_pct')}%")
        if reason:
            line += f"｜{reason}"
        detail_lines.append(line)
    if not detail_lines:
        return []
    return [f"持仓 {len(detail_lines)} 只 | 总盈亏 {data.get('summary', {}).get('total_pnl_pct', '—')}%", *detail_lines]


def _watch_price_lines(params: Optional[dict] = None) -> list[str]:
    """自选价格阈值触发 → 文本行。"""
    try:
        from StockInvestmentTool.notifier.notify import NotifyRules, build_price_messages
        from StockInvestmentTool.screener.sources import tencent_quotes

        params = params or {}
        rules = NotifyRules.from_yaml()
        codes = [w["code"] for w in rules.watchlist if w.get("code")]
        if not codes:
            return []
        quotes = tencent_quotes(codes).to_dict("records")
        if not params:
            return build_price_messages(rules, quotes)
        direction = str(params.get("direction", "up")).lower()
        threshold = float(params.get("pct", 0))
        lines = []
        for quote in quotes:
            change = float(quote.get("change_pct") or 0)
            matched = change >= threshold if direction == "up" else change <= -threshold
            if matched:
                lines.append(f"{quote.get('name') or quote.get('code')}: 涨跌幅 {change:+.2f}%（阈值 {direction} {threshold:.2f}%）")
        return lines
    except Exception as e:
        logger.warning("价格阈值检查失败: %s", e)
        return []


def _reload_scheduler_jobs(app) -> None:
    """免重启：保存通知配置后重挂触发器作业，并按 Active Config 重载数据任务。"""
    sched = app.extensions.get("scheduler") if app else None
    if sched is None:
        return
    try:
        _schedule_from_triggers(sched)
        _reload_data_scheduler_jobs(app)
    except Exception as e:
        logger.error("重挂通知触发器失败: %s", e)


# ── 通知去重（避免盘中每10分钟重复推送同一建议）──────────────
_NOTIFY_STATE = os.path.join(os.environ.get("STOCK_OUTPUT_DIR", ""), "data", "notify_state.json") \
    if os.environ.get("STOCK_OUTPUT_DIR") else None
# 去重窗口（小时）：同一持仓同一建议类型在此窗口内不重复推送
_DEDUP_HOURS = 24
_INTRADAY_DAILY_LIMIT = 3


def _intraday_trigger_quota(rule: dict) -> dict:
    """Return a persistent daily quota for ordinary intraday trigger delivery."""
    from datetime import datetime as _dt

    state = _load_notify_state()
    now = _dt.now()
    limit = max(1, int(os.getenv("INTRADAY_NOTIFY_DAILY_LIMIT", str(_INTRADAY_DAILY_LIMIT))))
    key = f"trigger_count:{rule.get('id') or rule.get('name') or 'intraday'}:{now:%Y-%m-%d}"
    count = int(state.get(key, 0) or 0)
    return {"allowed": count < limit, "state": state, "key": key,
            "count": count, "limit": limit}


def _commit_intraday_trigger_quota(quota: dict) -> None:
    state = dict(quota["state"])
    state[quota["key"]] = int(quota["count"]) + 1
    _save_notify_state(state)


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
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
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
    day_key = f"intraday_count:{now:%Y-%m-%d}"
    used = int(state.get(day_key, 0) or 0)
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
        # sell_all is an urgent risk exit and must not be hidden by the normal
        # intraday daily cap. Other recommendations share one daily quota.
        urgent = adv.get("advice_type") == "sell_all"
        if not dedup and (urgent or used < _INTRADAY_DAILY_LIMIT):
            kept_positions.append(p)
            if not urgent:
                used += 1
            # Do not persist yet. The caller commits only after a successful
            # channel delivery, otherwise a failed alert would be suppressed.
    data = dict(data)
    data["positions"] = kept_positions
    msgs = build_actionable_messages(data)
    return {"data": data, "messages": msgs,
            "pending_keys": [
                f"{p.get('id')}:{(p.get('advice') or {}).get('advice_type')}"
                for p in kept_positions
            ], "state": state, "now": now.isoformat(timespec="seconds"),
            "day_key": day_key, "daily_count": used}


def _commit_notify_dedup(dedup: dict) -> None:
    """Persist pending notification keys after the channel confirms success."""
    state = dict(dedup.get("state") or {})
    stamp = dedup.get("now")
    for key in dedup.get("pending_keys") or []:
        state[key] = stamp
    if dedup.get("day_key"):
        state[dedup["day_key"]] = int(dedup.get("daily_count", 0))
    _save_notify_state(state)


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
            _commit_notify_dedup(dedup)
            logger.info("持仓操作提醒已推送邮件（%d 只有操作建议）",
                        len(dedup["messages"]))
        else:
            images = _build_snapshot_images(dedup["data"])
            sent = send_all(rules.channel, webhook, dedup["messages"], images=images)
            _commit_notify_dedup(dedup)
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
    from StockInvestmentTool.ops.job_runs import JobRunStore
    run_id = JobRunStore().start("online_snapshot")
    try:
        result = run_online_snapshot()
        if result.get("ok"):
            logger.info("盘中快照已采集: %s", result.get("path"))
        else:
            logger.warning("盘中快照失败: %s", result.get("error"))
        JobRunStore().finish(run_id, "success" if result.get("ok") else "failed", result)
    except Exception as e:
        logger.error("盘中快照任务异常: %s", e)
        JobRunStore().finish(run_id, "failed", error=str(e))


def run_minute_snapshot_job():
    """盘中分钟数据任务主体（默认关闭，观察池范围）。"""
    from StockInvestmentTool.warehouse.online import _default_observe_codes
    from StockInvestmentTool.warehouse.minute import collect_minute_snapshot
    from StockInvestmentTool.ops.job_runs import JobRunStore
    run_id = JobRunStore().start("minute_snapshot")
    try:
        result = collect_minute_snapshot(_default_observe_codes())
        if result.get("ok"):
            logger.info("分钟数据已采集: %s (%d codes/%d rows)",
                        result.get("path"), result.get("codes", 0), result.get("rows", 0))
        else:
            logger.warning("分钟数据采集失败: %s", result.get("errors"))
        JobRunStore().finish(run_id, "success" if result.get("ok") else "failed", result)
    except Exception as e:
        logger.error("分钟数据任务异常: %s", e)
        JobRunStore().finish(run_id, "failed", error=str(e))


def run_system_alert_job() -> dict:
    """Evaluate health and enqueue deduplicated system alerts."""
    from StockInvestmentTool.ops.freshness import data_status
    from StockInvestmentTool.notifier.system_alerts import enqueue_alerts
    status = data_status()
    from StockInvestmentTool.notifier.outbox import NotificationOutbox
    status["notification_health"] = NotificationOutbox().counts()
    ids = enqueue_alerts(status)
    return {"alerts": len(ids), "outbox_ids": ids, "overall_status": status.get("overall_status")}


def scheduler_status(app) -> dict:
    """定时任务状态（管理页展示）。"""
    sched = app.extensions.get("scheduler")
    if sched is None:
        return {"enabled": False}
    jobs = sched.get_jobs()
    next_run = str(jobs[0].next_run_time)[:16] if jobs else None
    return {"enabled": True, "next_run": next_run,
            "spec": os.getenv("DAILY_RUN_TIME", DEFAULT_RUN_TIME)}
