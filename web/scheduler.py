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


def _daily_timeout() -> float:
    raw = os.getenv("WAREHOUSE_DAILY_TIMEOUT", "1800")
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
        return value
    except (TypeError, ValueError):
        logger.warning("WAREHOUSE_DAILY_TIMEOUT 格式错误(%s)，使用默认 1800 秒", raw)
        return 1800.0


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

    # ⑥ 持仓运行状态评估（日线收盘后）：让日线收盘价计入后高，
    #     并触发用户配置的目标价规则（盘中已由分钟任务评估过，这里补日线口径）。
    try:
        results["position_runtime"] = _evaluate_position_runtime()
    except Exception as e:
        logger.error("持仓运行状态评估失败: %s", e)
        failures.append("position_runtime")

    results["failures"] = failures
    if owns_run or run_id:
        JobRunStore().finish(run_id, "failed" if failures else "success", results)
    logger.info("=== 每日自动任务完成: %s ===", results)
    return results


def run_daily_digest(mgr=None) -> dict:
    """Build one post-close digest from price/fundflow/daily/order topics."""
    from StockInvestmentTool.biz.daily_digest import build_daily_digest
    return build_daily_digest()


def run_warehouse_daily() -> dict:
    """数据仓库每日离线采集：仅采集当前明确日期。"""
    logger.info("=== 数据仓库离线采集开始 ===")
    from StockInvestmentTool.warehouse.collector import MarketCollector

    result = {}
    c = MarketCollector()
    start_date = datetime.now().strftime("%Y-%m-%d")
    end_date = start_date
    sync_res = c.sync_daily(
        start_date=start_date, end_date=end_date,
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

        # Scheduled collection is an incremental daily job. Historical
        # backfills must use an explicit, operator-supplied date range.
        start_date = run_date
        end_date = run_date
        child_statuses = []
        store.update_progress(run_id, phase="获取股票清单", progress=1)
        result["daily"] = MarketCollector().sync_daily(
            start_date=start_date, end_date=end_date, include_etf=True,
            include_index=False, source="tencent", progress_callback=progress,
            job_run_id=run_id,
            timeout=_daily_timeout(),
        )
        if os.getenv("WAREHOUSE_PIPELINE_BUILD") == "1" and result["daily"].get("source_batch_id"):
            result["published_daily"] = publish_daily_batch(result["daily"]["source_batch_id"], run_id)
        if _has_enabled_auxiliary_tasks():
            result["auxiliary"] = run_auxiliary_data_pipeline(parent_run_id=run_id)
        daily_status = store.result_status(result["daily"])
        child_statuses.append(daily_status)
        if daily_status in {"failed", "timeout"}:
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
    from StockInvestmentTool.warehouse.industry import IndustryCollector, stage_and_publish_industry_batch
    from StockInvestmentTool.warehouse.storage import Warehouse
    from StockInvestmentTool.ops.task_center import TaskCenter, management_db_path
    from StockInvestmentTool.fundflow.capture import capture_money_flow

    warehouse = Warehouse()
    for name in ("industry_membership", "fundamentals", "valuation_daily", "money_flow_daily"):
        warehouse.metadata.register_dataset(name)
    configured = TaskCenter(management_db_path()).active_configs()
    result = {"enabled_tasks": []}
    collector = FundamentalsCollector(warehouse=warehouse)
    if _task_schedule_enabled(configured, "industry_capture"):
        try:
            industry = IndustryCollector(warehouse).collect_membership(
                snapshot_date=datetime.now().strftime("%Y-%m-%d"))
            if industry.get("raw_batch_id"):
                industry["published"] = stage_and_publish_industry_batch(
                    warehouse, dataset_name="industry_membership",
                    batch_id=industry["raw_batch_id"],
                    expected_symbols=industry.get("expected_symbols"),
                )
            result["industry"] = industry
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
    _schedule_business_tasks(scheduler, app)
    # 兜底：定期回收过期任务锁/在途状态，防止异常退出后任务永久卡死。
    scheduler.add_job(
        _recover_stale_task_state, CronTrigger(minute="*/30", timezone=TZ),
        id="recover_stale_state", misfire_grace_time=1800, coalesce=True,
        max_instances=1,
    )
    # 封盘后按小时重试未完成的采集链，直到发布成功或当天下线。
    scheduler.add_job(
        _post_close_retry_safe, CronTrigger(hour="16-23", minute="0", timezone=TZ),
        id="post_close_retry", misfire_grace_time=3600, coalesce=True,
        max_instances=1,
    )
    logger.info("封盘后采集重试已注册: 每天 16:00 起每小时")
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

    # No worker thread from a previous web process can survive this startup.
    # Reclaim every run older than this process immediately, not after an
    # arbitrary grace window, so a restart never leaves a false running task.
    _recover_stale_task_state(before=startup_boundary)

    scheduler.start()
    app.extensions["scheduler"] = scheduler
    app.extensions["scheduler_state"] = {"running": True, "reason": "registered", "startup_at": startup_boundary.isoformat(timespec="seconds")}
    app.extensions["scheduler_lock"] = lock_file
    if lock_file is not None:
        import atexit
        atexit.register(lock_file.close)
    logger.info("每日定时任务已启动: %02d:%02d (%s)", hour, minute, TZ)


def _today_text() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _schedule_business_tasks(scheduler, app=None) -> None:
    """Schedule only implemented business maintenance tasks.

    The callback persists a BusinessRequest/BusinessJobRun; the dedicated
    business-worker container performs the actual work.
    """
    if os.getenv("BUSINESS_SCHEDULER_ENABLED", "1") != "1":
        logger.info("业务任务调度已关闭: BUSINESS_SCHEDULER_ENABLED!=1")
        if app is not None:
            app.extensions["business_scheduler_state"] = {
                "enabled": False, "registered": [], "reason": "disabled_by_config",
            }
        return
    try:
        from StockInvestmentTool.biz.scheduler import BusinessScheduler

        minutes = max(1, int(os.getenv("BUSINESS_EXPIRY_RECONCILE_MINUTES", "30")))
        business = BusinessScheduler(scheduler)
        business.register_interval("observation.expiry_reconcile", minutes=minutes)
        # 通知投递：领取并发送 pending 投递（与旧 outbox 每 5 分钟节奏一致）
        outbox_minutes = max(1, int(os.getenv("NOTIFICATION_OUTBOX_MINUTES", "5")))
        business.register_interval("notification.outbox_delivery", minutes=outbox_minutes)
        # 每日盘后汇总：收盘后生成日报 + 通知事件
        digest_time = os.getenv("BUSINESS_DIGEST_TIME", "15:35")
        try:
            digest_hh, digest_mm = (int(v) for v in digest_time.split(":"))
        except (TypeError, ValueError):
            digest_hh, digest_mm = 15, 35
        business.register_cron(
            "report.daily_generate",
            hour=digest_hh, minute=digest_mm,
            input_data={"report_date": _today_text(), "notify": True},
        )
        if app is not None:
            app.extensions["business_scheduler"] = business
            app.extensions["business_scheduler_state"] = {
                "enabled": True, "registered": business.registered(),
                "reason": "registered", "expiry_reconcile_minutes": minutes,
                "outbox_delivery_minutes": outbox_minutes,
                "digest_time": digest_time,
            }
        logger.info("业务维护任务已注册: observation.expiry_reconcile 每 %d 分钟, "
                    "notification.outbox_delivery 每 %d 分钟, report.daily_generate %s",
                    minutes, outbox_minutes, digest_time)
    except Exception as exc:  # noqa: BLE001
        logger.exception("业务任务调度注册失败")
        if app is not None:
            app.extensions["business_scheduler_state"] = {
                "enabled": False, "registered": [],
                "reason": "registration_failed", "error": str(exc),
            }


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
        trading_date = _today_text()
        payload = {
            "trigger_type": "scheduled", "requested_by": "scheduler",
            "period_start": trading_date, "period_end": trading_date,
        }
        if task_key == "stock_daily_capture":
            payload["task_timeout"] = _daily_timeout()
            configured_keys = TaskCenter(management_db_path()).active_configs()
        elif task_key == "valuation_capture":
            # 估值是全市场逐只串行，设置批次超时，避免阻塞 web worker。
            payload["task_timeout"] = _daily_timeout()
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


def _recover_stale_task_state(*, before: datetime | None = None) -> int:
    """启动时回收遗留任务状态（进程重启后的残留在途状态）。

    回收 running JobRun、过期任务锁、running SourceBatch，并标记残留
    publishing 版本，避免重启后重复写入或永久卡死。
    """
    recovered = 0
    from datetime import datetime
    from StockInvestmentTool.ops.job_runs import JobRunStore
    store = JobRunStore()
    boundary = before or (datetime.now() - timedelta(minutes=5))
    recovered += store.reclaim_data_running(before=boundary)
    # 回收更宽时间窗内的孤儿运行记录，避免异常退出后任务永久 running。
    orphan = store.cancel_orphan_running(
        job_names=[
            "daily_tasks", "daily_sync", "stock_daily_capture", "stock_daily_build",
            "stock_daily_quality", "stock_daily_publish", "indicators_build",
            "rebuild_indicators", "rebuild_factors", "valuation_capture",
            "industry_daily_capture", "industry_features_build", "industry_rotation_build",
        ],
        before=(before or (datetime.now() - timedelta(minutes=60))).isoformat(timespec="seconds"))
    recovered += orphan["jobs"] + orphan["plans"]
    recovered += store.recover_stale_locks()
    try:
        from StockInvestmentTool.warehouse.source_batches import SourceBatchStore
        from StockInvestmentTool.warehouse.storage import Warehouse
        recovered += SourceBatchStore(Warehouse().meta_db_path).recover_running(before=boundary)
    except Exception as exc:
        logger.warning("SourceBatch 遗留状态回收失败: %s", exc)
    try:
        from StockInvestmentTool.ops.task_center import TaskCenter, management_db_path
        center = TaskCenter(management_db_path())
        recovered += center.recover_inflight_requests(
            before=boundary.isoformat(timespec="seconds"))
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


def _dataset_released(dataset_name: str, target_date: str) -> bool:
    """Return whether the dataset already has a published row for target_date.

    Only the current published version matters; candidate/failed versions are
    ignored so a retry is idempotent.
    """
    from StockInvestmentTool.warehouse.datasets import DatasetAccess, DatasetAccessError
    from StockInvestmentTool.warehouse.storage import Warehouse
    try:
        access = DatasetAccess(Warehouse())
        current = access.get_current_version(dataset_name)
        if not current:
            return False
        frame = access.load_dataset(dataset_name, start_date=target_date,
                                    end_date=target_date, required_quality="WARNING").data
        if frame is None or frame.empty:
            return False
        date_col = "date" if "date" in frame.columns else ("trading_date" if "trading_date" in frame.columns else None)
        if not date_col:
            return True
        import pandas as pd
        return pd.to_datetime(frame[date_col], errors="coerce").max().strftime("%Y-%m-%d") >= target_date
    except (DatasetAccessError, Exception):  # noqa: BLE001
        return False


def run_post_close_retry_job() -> dict:
    """封盘后按小时重试未完成的采集链，直到发布成功或当天下线。

    只重试当前交易日，幂等：某数据集当日已发布则跳过，不会重复覆盖历史分区。
    一次失败不抛异常，保持调度器存活，交由下一轮小时触发再试。
    """
    from StockInvestmentTool.ops.task_center import TaskCenter, management_db_path
    from StockInvestmentTool.ops.task_execution import execute_task, execute_pipeline
    trading_date = _today_text()
    configured = TaskCenter(management_db_path()).active_configs()
    result = {"date": trading_date, "retried": []}

    def enabled(key):
        return bool((configured.get(key) or {}).get("enabled"))

    # 日线采集链：st 日线未发布则重试整条链。
    daily_chain = [k for k in ("stock_daily_capture", "stock_daily_build", "stock_daily_quality",
                               "stock_daily_publish", "indicators_build") if enabled(k)]
    if daily_chain and not _dataset_released("stock_daily", trading_date):
        payload = {"trigger_type": "retry", "requested_by": "scheduler",
                   "period_start": trading_date, "period_end": trading_date,
                   "task_timeout": _daily_timeout()}
        try:
            result["daily"] = execute_pipeline(management_db_path(), daily_chain, payload)
            result["retried"].append("stock_daily_chain")
        except Exception as exc:  # noqa: BLE001
            result["daily_error"] = str(exc)

    # 同花顺行业指数：当日指数未发布则采集并发布，成功后触发行业特征重建。
    if enabled("industry_daily_capture") and not _dataset_released("industry_daily", trading_date):
        payload = {"trigger_type": "retry", "requested_by": "scheduler",
                   "period_start": trading_date, "period_end": trading_date,
                   "task_timeout": _daily_timeout()}
        try:
            captured = execute_task(management_db_path(), "industry_daily_capture", payload)
            result["industry_daily"] = captured
            result["retried"].append("industry_daily")
            full_result = captured.get("result") or {}
            # 采集只产出 Raw Batch；再走 Quality + Publish 后才可被下游读取。
            if full_result.get("raw_batch_id"):
                from StockInvestmentTool.warehouse.industry import stage_and_publish_industry_batch
                from StockInvestmentTool.warehouse.storage import Warehouse
                published = stage_and_publish_industry_batch(
                    Warehouse(meta_db_path=management_db_path()),
                    dataset_name="industry_daily", batch_id=full_result["raw_batch_id"],
                    expected_symbols=full_result.get("expected_symbols"),
                    partition=trading_date[:7])
                result["industry_daily_publish"] = published
            if enabled("industry_features_build") and _dataset_released("industry_daily", trading_date):
                result["industry_features"] = execute_task(
                    management_db_path(), "industry_features_build",
                    {"trigger_type": "retry", "requested_by": "scheduler",
                     "period_start": trading_date, "period_end": trading_date, "as_of": trading_date})
                result["retried"].append("industry_features_build")
        except Exception as exc:  # noqa: BLE001
            result["industry_daily_error"] = str(exc)
    if (enabled("industry_rotation_build") and _dataset_released("industry_daily", trading_date)
            and not _dataset_released("industry_rotation_daily", trading_date)):
        try:
            result["industry_rotation"] = execute_task(
                management_db_path(), "industry_rotation_build",
                {"trigger_type": "retry", "requested_by": "scheduler",
                 "period_start": trading_date, "period_end": trading_date, "as_of": trading_date})
            result["retried"].append("industry_rotation_build")
        except Exception as exc:  # noqa: BLE001
            result["industry_rotation_error"] = str(exc)
    return result


def _post_close_retry_safe() -> None:
    """Scheduler job wrapper so a retry failure never crashes the scheduler."""
    try:
        run_post_close_retry_job()
    except Exception as exc:  # noqa: BLE001
        logger.error("封盘后采集重试异常: %s", exc)


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

    读取最新 triggers（biz/notify_rules.yaml），每次保存后调用
    `_reload_scheduler_jobs` 重建作业，无需重启容器。
    """
    from StockInvestmentTool.biz.triggers import enabled_triggers, run_trigger_rule
    from apscheduler.triggers.cron import CronTrigger

    # 去掉旧的触发器作业（保留 daily_tasks / online_snapshot）
    for job in scheduler.get_jobs():
        if job.id in ("actionable_monitor", "post_close_summary"):
            job.remove()
        elif job.id.startswith("trigger_"):
            job.remove()

    for rule in enabled_triggers():
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
    """执行一条触发器规则：按条件过滤持仓/信号，聚合发送（biz 实现）。"""
    from StockInvestmentTool.biz.triggers import run_trigger_rule as _biz_run
    return _biz_run(rule)


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
    """盘中分钟数据任务主体（默认关闭，观察池范围）。

    分钟数据落盘成功后立即执行持仓运行状态评估（回撤通知），
    保证「数据拉到 → 计算」紧耦合、不另起定时。
    """
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
        if result.get("ok"):
            _evaluate_position_runtime()
    except Exception as e:
        logger.error("分钟数据任务异常: %s", e)
        JobRunStore().finish(run_id, "failed", error=str(e))


def _evaluate_position_runtime() -> dict:
    """分钟数据落盘后执行持仓运行状态评估（回撤通知）。

    独立函数便于手动触发与测试；失败只记录日志，不影响分钟任务本身。
    """
    try:
        from StockInvestmentTool.biz.position_runtime import PositionRuntimeService
        summary = PositionRuntimeService().evaluate_all()
        logger.info("持仓运行状态评估完成: 评估 %d 只, 触发回撤通知 %d 只",
                    summary["evaluated"], summary["triggered"])
        return summary
    except Exception as exc:  # noqa: BLE001
        logger.error("持仓运行状态评估失败: %s", exc)
        return {"evaluated": 0, "triggered": 0, "error": str(exc)}


def scheduler_status(app) -> dict:
    """定时任务状态（管理页展示）。"""
    sched = app.extensions.get("scheduler")
    if sched is None:
        return {"enabled": False}
    jobs = sched.get_jobs()
    next_run = str(jobs[0].next_run_time)[:16] if jobs else None
    return {"enabled": True, "next_run": next_run,
            "spec": os.getenv("DAILY_RUN_TIME", DEFAULT_RUN_TIME)}
