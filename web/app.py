"""StockInvestmentTool Web 前端 — Flask 应用

使用统一 AnalysisEngine，与 CLI 共享分析管线。
"""

import json
import logging
import os
from urllib.parse import urlparse
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

import flask
import numpy as np
import pandas as pd

# 确保包路径可访问
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from StockInvestmentTool.config import Config
from StockInvestmentTool.core.engine import AnalysisEngine, AnalysisOptions
from StockInvestmentTool.core.registry import SchemeRegistry
from StockInvestmentTool.datasource.fetcher import StockDataFetcher
from StockInvestmentTool.prompt.llm_client import DeepSeekClient, LLMError
from StockInvestmentTool.runtime.memory import memory_snapshot, monitor_memory

logger = logging.getLogger(__name__)


def _to_json_safe(obj):
    """递归将 numpy 类型转为 Python 原生类型"""
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_json_safe(v) for v in obj]
    return obj


# ── Flask 应用 ────────────────────────────────────

web_app = flask.Blueprint("stock_web", __name__, template_folder="templates")

# 在线程中存储分析进度
_analysis_status: dict[str, dict] = {}
_heavy_task_lock = threading.BoundedSemaphore(1)


def _attach_memory(status: dict, label: str, memory_result: dict) -> None:
    """Expose task memory in the response while keeping logs concise."""
    result = memory_result.get("state", {}).get("result")
    if result:
        status["memory"] = result


def _run_analysis(task_id: str, code: str, name: str,
                  start_date: str, end_date: str,
                  do_backtest: bool, do_prompt: bool, do_api: bool,
                  initial_cash: float = 100000,
                  scheme_name: str = "default_value",
                  stock_type: str = "B"):
    """后台执行分析流程（通过统一 AnalysisEngine）"""
    status = _analysis_status[task_id]
    with _heavy_task_lock:
        with monitor_memory(f"analysis:{task_id}") as memory:
            try:
                status["stage"] = "初始化引擎"
                status["progress"] = 5
                engine = AnalysisEngine(scheme_name)

                status["stage"] = "获取数据"
                status["progress"] = 10

                options = AnalysisOptions(
                    do_backtest=do_backtest,
                    do_prompt=do_prompt,
                    do_api=do_api,
                    skip_charts=False,
                    stock_type=stock_type,
                    initial_cash=initial_cash if initial_cash else None,
                )

                result = engine.analyze(
                    code=code,
                    name=name,
                    start_date=start_date,
                    end_date=end_date,
                    options=options,
                    progress_callback=lambda stage, progress: status.update(
                        stage=stage, progress=progress
                    ),
                )

                status["stage"] = "生成结果"
                status["progress"] = 95

                result_data = result.to_dict()

                # 交易日期格式化（numpy datetime64 兼容）
                if "trades" in result_data:
                    for t in result_data["trades"]:
                        if "date" in t:
                            dt = t["date"]
                            if hasattr(dt, "strftime"):
                                t["date"] = dt.strftime("%Y-%m-%d")
                            else:
                                t["date"] = str(dt)[:10]

                # 报告内容用于前端展示
                if result.report_path:
                    try:
                        result_data["report_content"] = Path(result.report_path).read_text(encoding="utf-8")
                    except OSError:
                        pass

                status["result"] = result_data
                status["status"] = "success"
                status["stage"] = "完成"
                status["progress"] = 100

            except Exception as e:
                logger.exception("分析失败")
                status["status"] = "error"
                status["error"] = str(e)
                status["stage"] = "失败"
                status["progress"] = -1
            finally:
                # monitor_memory finalizes its result after this block exits.
                pass
        _attach_memory(status, "analysis", memory)


@web_app.route("/", methods=["GET"])
def index():
    """主页：投资工作台；旧的带股票参数链接兼容到个股分析。"""
    if flask.request.args.get("code") or flask.request.args.get("name"):
        return flask.redirect(flask.url_for("stock_web.analyze_page", **flask.request.args))
    return flask.render_template("workbench.html")


@web_app.route("/analyze", methods=["GET"])
def analyze_page():
    """个股分析表单（原首页页面）。"""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    last_year = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

    # 可用方案列表
    try:
        registry = SchemeRegistry()
        schemes = registry.list()
    except Exception as e:
        logger.warning("方案加载失败: %s", e)
        schemes = []

    return flask.render_template("index.html",
                                 yesterday=yesterday,
                                 last_year=last_year,
                                 schemes=schemes,
                                 has_api_key=bool(Config.DEEPSEEK_API_KEY),
                                 prefill_code=flask.request.args.get("code", ""),
                                 prefill_name=flask.request.args.get("name", ""),
                                  prefill_scheme=flask.request.args.get("scheme", ""))


@web_app.route("/schemes", methods=["GET"])
def schemes():
    """列出可用方案（JSON）"""
    try:
        registry = SchemeRegistry()
        data = []
        for s in registry.list():
            data.append({
                "name": s.name,
                "version": s.version,
                "description": s.description,
                "applicable_types": s.applicable_types,
            })
        return flask.jsonify({"status": "success", "schemes": data})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/schemes/raw", methods=["GET"])
def scheme_raw():
    """单个方案 YAML 原始内容（管理页编辑用）"""
    from StockInvestmentTool.portfolio import settings as s
    name = flask.request.args.get("name", "")
    try:
        return flask.jsonify({"status": "success", "name": name, "content": s.read_scheme(name)})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 404


# ── FR-2 策略编排器 API ────────────────────────────────────

@web_app.route("/api/indicators", methods=["GET"])
def api_indicators():
    """列出指标体系（基础/组合/代码），供策略编排器引用（I1）。"""
    from StockInvestmentTool.indicators.engine import IndicatorRegistry
    try:
        reg = IndicatorRegistry()
        custom = {x["name"]: x for x in __import__(
            "StockInvestmentTool.indicators.store", fromlist=["list_indicators"]
        ).list_indicators()}
        groups = {"base": [], "composite": [], "code": []}
        for name in reg.all_names():
            d = reg.get(name)
            if d is None:
                continue
            kind = d.kind if d.kind in groups else "composite"
            groups[kind].append({
                "name": d.name,
                "kind": d.kind,
                "expr": d.expr,
                "description": d.description,
                "applies_to": d.applies_to,
                "source": custom.get(name, {}).get("source", "builtin"),
                "editable": bool(custom.get(name, {}).get("editable", False)),
                "enabled": custom.get(name, {}).get("enabled", True),
                **__import__("StockInvestmentTool.indicators.documentation", fromlist=["documentation_for"]).documentation_for(d.name, fallback=d.description),
            })
        # Keep disabled custom indicators visible in the management catalogue.
        existing = {item["name"] for group in groups.values() for item in group}
        for item in custom.values():
            if item["name"] in existing:
                continue
            item = {**item, **__import__("StockInvestmentTool.indicators.documentation", fromlist=["documentation_for"]).documentation_for(item["name"], fallback=item.get("description", ""))}
            groups.setdefault(item.get("kind", "composite"), []).append(item)
        count = sum(len(items) for items in groups.values())
        return flask.jsonify({"status": "success", "groups": groups,
                              "count": count})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/health/details", methods=["GET"])
def api_health_details():
    """Operational readiness details for authenticated administrators."""
    from StockInvestmentTool.config import Config
    from StockInvestmentTool.warehouse.storage import Warehouse
    try:
        warehouse = Warehouse()
        daily_months = warehouse.available_months("daily")
        minute_days = warehouse.minute_store().days()
        scheduler = flask.current_app.extensions.get("scheduler")
        jobs = scheduler.get_jobs() if scheduler else []
        from StockInvestmentTool.notifier.outbox import NotificationOutbox
        from StockInvestmentTool.ops.job_runs import JobRunStore
        outbox = NotificationOutbox()
        return flask.jsonify({
            "status": "success",
            "scheduler": {"enabled": bool(scheduler), "jobs": len(jobs)},
            "warehouse": {"daily_partitions": len(daily_months), "minute_days": len(minute_days)},
            "notifications": {"outbox": outbox.counts(), "recent": outbox.recent(5)},
            "jobs": {"recent": JobRunStore().recent(10)},
            "features": {
                "minute_snapshot": os.getenv("WAREHOUSE_MINUTE_SNAPSHOT") == "1",
                "daily_sync": os.getenv("WAREHOUSE_DAILY_SYNC") == "1",
                "online_snapshot": os.getenv("WAREHOUSE_ONLINE_SNAPSHOT") == "1",
                "intraday_notify_limit": int(os.getenv("INTRADAY_NOTIFY_DAILY_LIMIT", "3")),
                "auth_disabled": os.getenv("STOCK_DISABLE_AUTH") == "1",
                "auth": bool(os.getenv("ADMIN_PASSWORD")),
            },
            "paths": {"data_dir": str(Config.DATA_DIR)},
            "memory": memory_snapshot(),
        })
    except Exception as e:
        logger.exception("健康详情读取失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/data/status", methods=["GET"])
def api_data_status():
    """Return explainable freshness status for each warehouse dataset."""
    try:
        from StockInvestmentTool.ops.freshness import data_status
        result = data_status()
        from StockInvestmentTool.notifier.outbox import NotificationOutbox
        result["notification_health"] = NotificationOutbox().counts()
        return flask.jsonify(result)
    except Exception as e:
        logger.exception("数据状态读取失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/data/jobs", methods=["GET"])
def api_data_jobs():
    """Read the durable job ledger used by the data center."""
    try:
        from StockInvestmentTool.ops.job_runs import JobRunStore
        raw_page = flask.request.args.get("page")
        raw_size = flask.request.args.get("page_size")
        raw_limit = flask.request.args.get("limit")
        for param, raw in (("page", raw_page), ("page_size", raw_size), ("limit", raw_limit)):
            if raw is not None and not raw.isdigit():
                return flask.jsonify({"status": "error", "error": f"{param} 必须为整数"}), 400
        if raw_limit is not None and int(raw_limit) < 1:
            return flask.jsonify({"status": "error", "error": "limit 必须为正整数"}), 400
        limit = max(1, min(int(raw_limit or raw_size or 50), 200))
        if raw_page is not None and int(raw_page) < 1:
            return flask.jsonify({"status": "error", "error": "page 必须为正整数"}), 400
        if raw_page is not None and int(raw_page) > 1_000_000:
            return flask.jsonify({"status": "error", "error": "page 超出允许范围"}), 400
        if raw_size is not None and int(raw_size) < 1:
            return flask.jsonify({"status": "error", "error": "page_size 必须为正整数"}), 400
        page = max(1, int(raw_page or 1))
        category = flask.request.args.get("category", "all")
        if category not in JobRunStore.CATEGORIES:
            return flask.jsonify({"status": "error", "error": "非法任务类别"}), 400
        store = JobRunStore()
        if category == "data":
            store.ensure_daily_plan(daily_time=os.getenv("DAILY_RUN_TIME", "15:35"))
        name = flask.request.args.get("job_name")
        status = flask.request.args.get("status")
        items, total = store.query(limit=limit, offset=(page - 1) * limit,
                                   job_names=[name] if name else None, status=status,
                                   category=category)
        return flask.jsonify({"status": "success", "jobs": items, "total": total,
                              "page": page, "page_size": limit, "has_more": page * limit < total,
                              "category": category})
    except (TypeError, ValueError):
        return flask.jsonify({"status": "error", "error": "分页参数必须为整数"}), 400
    except OverflowError:
        return flask.jsonify({"status": "error", "error": "分页参数超出范围"}), 400
    except Exception as e:
        logger.exception("任务台账读取失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/data/plan", methods=["GET"])
def api_data_plan():
    from StockInvestmentTool.ops.job_runs import JobRunStore
    store = JobRunStore()
    scheduler = flask.current_app.extensions.get("scheduler")
    real_jobs = [{"id": j.id, "trigger": str(j.trigger), "next_run": str(j.next_run_time) if j.next_run_time else None}
                 for j in scheduler.get_jobs()] if scheduler else []
    return flask.jsonify({"status": "success", "run_date": flask.request.args.get("date") or datetime.now().strftime("%Y-%m-%d"),
                          "scheduler_jobs": real_jobs,
                          "tasks": store.ensure_daily_plan(run_date=flask.request.args.get("date"), daily_time=os.getenv("DAILY_RUN_TIME", "15:35"))})


@web_app.route("/api/data/scheduler", methods=["GET"])
def api_data_scheduler():
    scheduler = flask.current_app.extensions.get("scheduler")
    scheduler_state = flask.current_app.extensions.get("scheduler_state") or {}
    daily = os.getenv("WAREHOUSE_DAILY_SYNC") == "1"
    minute = os.getenv("WAREHOUSE_MINUTE_SNAPSHOT") == "1"
    online = os.getenv("WAREHOUSE_ONLINE_SNAPSHOT") == "1"
    effective = "minute" if minute else "online" if online else None
    jobs = [{"id": j.id, "trigger": str(j.trigger), "next_run": str(j.next_run_time) if j.next_run_time else None}
            for j in scheduler.get_jobs()] if scheduler else []
    running = bool(scheduler)
    base_reason = scheduler_state.get("reason", "registered" if running else "scheduler_not_running")
    def feature(configured, registered, eligible, suppressed=False):
        if not configured: reason = "disabled_by_config"
        elif suppressed: reason = "suppressed_by_minute"
        elif not running: reason = base_reason if base_reason != "not_initialized" else "scheduler_not_running"
        elif not eligible: reason = "not_registered"
        elif not registered: reason = "not_registered"
        else: reason = "registered"
        return {"configured": configured, "eligible": eligible, "effective": running and registered,
                "registered": registered, "reason": reason}
    return flask.jsonify({"status": "success", "running": running,
        "reason": base_reason if not running else "registered",
        "timezone": "Asia/Shanghai", "features": {
            "daily_sync": feature(daily, any(j["id"] == "daily_sync" for j in jobs), daily),
            "minute_snapshot": feature(minute, any(j["id"].startswith("minute_snapshot") for j in jobs), minute),
            "online_snapshot": feature(online, any(j["id"] == "online_snapshot" for j in jobs), online and not minute, minute and online)},
        "jobs": jobs})


@web_app.route("/api/data/jobs/<int:run_id>", methods=["GET"])
def api_data_job(run_id):
    from StockInvestmentTool.ops.job_runs import JobRunStore
    item = JobRunStore().get(run_id)
    if item is None:
        return flask.jsonify({"status": "error", "error": "任务运行记录不存在"}), 404
    from StockInvestmentTool.ops.terminology import task_labels
    return flask.jsonify({"status": "success", "job": task_labels(item)})


@web_app.route("/api/tasks", methods=["GET"])
def api_tasks():
    """List declarative task definitions for the task center."""
    from StockInvestmentTool.ops.task_center import TaskCenter
    from StockInvestmentTool.config import Config
    center = _task_center_service()
    center.sync_definitions()
    return flask.jsonify({"status": "success", "tasks": center.list_tasks()})


@web_app.route("/api/tasks/<task_key>", methods=["GET"])
def api_task_detail(task_key):
    from StockInvestmentTool.ops.task_center import TaskCenter
    center = _task_center_service()
    center.sync_definitions()
    task = center.task(task_key)
    if task is None:
        return flask.jsonify({"status": "error", "error": "任务不存在"}), 404
    from StockInvestmentTool.ops.terminology import task_labels
    return flask.jsonify({"status": "success", "task": task_labels(task)})


@web_app.route("/api/tasks/runs/<int:run_id>/events", methods=["GET"])
def api_task_events(run_id):
    from StockInvestmentTool.ops.task_center import TaskCenter
    after = flask.request.args.get("after", "0")
    limit = flask.request.args.get("limit", "200")
    try:
        events = _task_center_service().events(run_id, limit=int(limit), after_id=int(after))
    except (TypeError, ValueError):
        return flask.jsonify({"status": "error", "error": "事件分页参数无效"}), 400
    return flask.jsonify({"status": "success", "events": events})


@web_app.route("/api/tasks/runs/<int:run_id>/logs", methods=["GET"])
def api_task_logs(run_id):
    from StockInvestmentTool.ops.task_center import TaskCenter
    center = _task_center_service()
    path = center.db_path.parent / "task_logs" / f"{run_id}.log"
    if not path.exists():
        return flask.jsonify({"status": "success", "run_id": run_id, "log": "", "next_offset": 0})
    try:
        offset = max(0, int(flask.request.args.get("offset", "0")))
        limit = max(1, min(int(flask.request.args.get("limit", "200")), 1000))
    except ValueError:
        return flask.jsonify({"status": "error", "error": "日志分页参数无效"}), 400
    lines = path.read_text(encoding="utf-8").splitlines()
    selected = lines[offset:offset + limit]
    return flask.jsonify({"status": "success", "run_id": run_id, "log": "\n".join(selected),
                          "next_offset": offset + len(selected), "has_more": offset + len(selected) < len(lines)})


@web_app.route("/api/tasks/runs/<int:run_id>/artifacts", methods=["GET"])
def api_task_artifacts(run_id):
    from StockInvestmentTool.ops.task_center import TaskCenter
    return flask.jsonify({"status": "success", "artifacts": _task_center_service().artifacts(run_id=run_id)})


@web_app.route("/api/artifacts/<artifact_id>/preview", methods=["GET"])
def api_artifact_preview(artifact_id):
    from StockInvestmentTool.ops.task_center import TaskCenter
    try:
        preview = _task_center_service().preview_artifact(
            artifact_id, limit=int(flask.request.args.get("limit", "50")),
            offset=int(flask.request.args.get("offset", "0")))
    except FileNotFoundError:
        return flask.jsonify({"status": "error", "error": "产物不存在"}), 404
    except (TypeError, ValueError) as exc:
        return flask.jsonify({"status": "error", "error": str(exc)}), 400
    return flask.jsonify({"status": "success", **_to_json_safe(preview)})


@web_app.route("/api/artifacts/<artifact_id>/lineage", methods=["GET"])
def api_artifact_lineage(artifact_id):
    from StockInvestmentTool.ops.task_center import TaskCenter
    direction = flask.request.args.get("direction", "both")
    try:
        result = _task_center_service().lineage(artifact_id, direction)
    except ValueError as exc:
        return flask.jsonify({"status": "error", "error": str(exc)}), 400
    return flask.jsonify({"status": "success", "artifact_id": artifact_id, **result})


@web_app.route("/api/metrics/catalog", methods=["GET"])
def api_metrics_catalog():
    from StockInvestmentTool.ops.task_center import TaskCenter
    center = _task_center_service()
    center.sync_definitions()
    center.sync_metrics()
    return flask.jsonify({"status": "success", "metrics": center.list_metrics()})


@web_app.route("/api/metrics/<metric_key>", methods=["GET"])
def api_metric_detail(metric_key):
    from StockInvestmentTool.ops.task_center import TaskCenter
    center = _task_center_service()
    center.sync_metrics()
    metric = next((item for item in center.list_metrics() if item["metric_key"] == metric_key), None)
    if metric is None:
        return flask.jsonify({"status": "error", "error": "指标不存在"}), 404
    return flask.jsonify({"status": "success", "metric": metric,
                          "artifacts": center.task_artifacts(metric.get("producer_task"))})


def _task_center_service():
    from StockInvestmentTool.ops.task_center import TaskCenter
    from StockInvestmentTool.ops.task_center import management_db_path
    path = management_db_path()
    if path.name == "management.db":
        return TaskCenter(path, path)
    return TaskCenter(path, Config.DATA_DIR / "warehouse" / "meta.db")


@web_app.route("/api/data-center/overview", methods=["GET"])
def api_data_center_overview():
    center = _task_center_service()
    center.sync_definitions()
    center.sync_metrics()
    assets = center.data_assets()
    counts = {"total": len(assets), "normal": 0, "attention": 0, "unknown": 0}
    for asset in assets:
        status = asset.get("health_status") or "unknown"
        if status in ("healthy", "normal"):
            counts["normal"] += 1
        elif status in ("partial", "stale", "critical"):
            counts["attention"] += 1
        else:
            counts["unknown"] += 1
    return flask.jsonify({"status": "success", "counts": counts,
                          "task": center.task_overview(),
                          "assets": assets})


@web_app.route("/api/data-center/assets", methods=["GET"])
def api_data_center_assets():
    center = _task_center_service()
    center.sync_metrics()
    assets = center.data_assets(category=flask.request.args.get("category"),
                                status=flask.request.args.get("status"),
                                date=flask.request.args.get("date"))
    return flask.jsonify({"status": "success", "assets": assets, "total": len(assets)})


@web_app.route("/api/data-center/assets/<metric_key>", methods=["GET"])
def api_data_center_asset_detail(metric_key):
    center = _task_center_service()
    center.sync_metrics()
    asset = next((item for item in center.data_assets() if item["metric_key"] == metric_key), None)
    if asset is None:
        return flask.jsonify({"status": "error", "error": "数据项不存在"}), 404
    applicability = {}
    if metric_key not in {"stock_daily", "indicators", "factors"}:
        from StockInvestmentTool.warehouse.asset_profiles import applicability as profile_applicability
        for asset_type in ("stock", "etf", "index"):
            applicability[asset_type] = profile_applicability(asset_type, "metrics", metric_key)
    return flask.jsonify({"status": "success", "asset": asset,
                          "applicability": applicability,
                          "tasks": center.task_artifacts(asset.get("producer_task"))})


@web_app.route("/api/task-center/overview", methods=["GET"])
def api_task_center_overview():
    center = _task_center_service()
    center.sync_definitions()
    return flask.jsonify({"status": "success", "overview": center.task_overview(),
                          "tasks": center.list_tasks()})


@web_app.route("/api/task-center/tasks", methods=["GET"])
def api_task_center_tasks():
    center = _task_center_service()
    center.sync_definitions()
    return flask.jsonify({"status": "success", "tasks": center.list_tasks()})


@web_app.route("/task-center", methods=["GET"])
def task_center_page():
    return flask.render_template("task_center.html")


def _start_data_job(job_name, worker):
    from StockInvestmentTool.ops.job_runs import JobRunStore
    store = JobRunStore()
    active = store.running(job_name)
    if active:
        return flask.jsonify({"status": "error", "error": "同一任务正在运行",
                              "run_id": active["id"]}), 409
    conflict_group = {"daily_sync", "rebuild_indicators", "rebuild_factors"}
    if job_name in conflict_group:
        active, _ = store.query(category="data", status="running", limit=200)
        if any(item["job_name"] in conflict_group and item.get("parent_run_id") is None for item in active):
            return flask.jsonify({"status": "error", "error": "离线数据任务正在运行"}), 409
    run_id = store.start(job_name, display_name={
        "daily_sync": "日线增量同步", "minute_snapshot": "观察池分钟采集",
        "rebuild_indicators": "指标重建", "rebuild_factors": "因子重建",
    }.get(job_name, job_name))
    if job_name in {"daily_sync", "rebuild_indicators", "rebuild_factors"}:
        store.ensure_daily_plan()
        store.link_plan_run(datetime.now().strftime("%Y-%m-%d"), job_name, run_id)

    def execute():
        try:
            result = worker(run_id)
            # Workers that own a ledger entry finish it themselves; otherwise
            # the dispatcher records the common success/failure transition.
            if store.get(run_id) and store.get(run_id)["status"] == "running":
                payload = result if isinstance(result, dict) else {"result": result}
                store.finish(run_id, store.result_status(payload), payload)
        except Exception as exc:
            logger.exception("手动任务失败: %s", job_name)
            store.finish(run_id, "failed", error=str(exc))

    threading.Thread(target=execute, daemon=True, name=f"job-{job_name}-{run_id}").start()
    return flask.jsonify({"status": "success", "run_id": run_id, "job_name": job_name}), 202


@web_app.route("/api/data/jobs/daily-sync", methods=["POST"])
def api_data_daily_sync():
    from StockInvestmentTool.web.scheduler import run_daily_data_pipeline
    return _start_data_job("daily_sync", lambda run_id: run_daily_data_pipeline(run_id))


@web_app.route("/api/data/jobs/minute-snapshot", methods=["POST"])
def api_data_minute_snapshot():
    from StockInvestmentTool.warehouse.online import _default_observe_codes
    from StockInvestmentTool.warehouse.minute import collect_minute_snapshot
    return _start_data_job("minute_snapshot", lambda _run_id: collect_minute_snapshot(_default_observe_codes()))


@web_app.route("/api/data/jobs/rebuild-indicators", methods=["POST"])
def api_data_rebuild_indicators():
    from StockInvestmentTool.warehouse.indicators_build import IndicatorsBuilder
    return _start_data_job("rebuild_indicators", lambda _run_id: IndicatorsBuilder(allow_legacy=False).build_all())


@web_app.route("/api/data/jobs/rebuild-factors", methods=["POST"])
def api_data_rebuild_factors():
    from StockInvestmentTool.warehouse.factors import FactorEngine
    return _start_data_job("rebuild_factors", lambda _run_id: FactorEngine(allow_legacy=False).build_factors())


@web_app.route("/data-center", methods=["GET"])
def data_center_page():
    return flask.render_template("data_center.html")


@web_app.route("/api/watch-pool", methods=["GET"])
def api_watch_pool():
    try:
        from StockInvestmentTool.portfolio.dashboard import DashboardService
        from StockInvestmentTool.ops.freshness import quick_daily_status
        refresh = flask.request.args.get("refresh") == "1"
        return flask.jsonify({"status": "success",
                              "data_health": quick_daily_status(),
                              "items": DashboardService(_get_manager()).watch_pool(refresh=refresh)})
    except Exception as e:
        logger.exception("观察池读取失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/watch-pool", methods=["GET"])
def watch_pool_page():
    return flask.render_template("watch_pool.html")


@web_app.route("/api/workbench/summary", methods=["GET"])
def api_workbench_summary():
    try:
        from StockInvestmentTool.ops.freshness import data_status
        from StockInvestmentTool.portfolio.dashboard import DashboardService
        manager = _get_manager()
        warroom = DashboardService(manager).war_room()
        pool = DashboardService(manager).watch_pool()
        health = data_status()
        return flask.jsonify({"status": "success", "market_summary": {},
                              "actionable_positions": [p for p in warroom.get("positions", []) if p.get("advice")],
                              "watchlist_changes": pool[:10], "dataset_health": health,
                              "notification_health": {},
                              "quick_actions": [{"label": "分析股票", "href": "/"},
                                                {"label": "查看观察池", "href": "/watch-pool"},
                                                {"label": "查看持仓", "href": "/dashboard/warroom"},
                                                {"label": "数据中心", "href": "/data-center"}]})
    except Exception as e:
        logger.exception("工作台摘要读取失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/workbench", methods=["GET"])
def workbench_page():
    return flask.render_template("workbench.html")


@web_app.route("/api/workbench/positions", methods=["GET"])
def api_workbench_positions():
    try:
        positions = [position.to_dict() for position in _get_manager().storage.get_open_positions()]
        return flask.jsonify({"status": "success", "positions": positions,
                              "count": len(positions)})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/workbench/notifications", methods=["GET"])
def api_workbench_notifications():
    try:
        from StockInvestmentTool.notifier.outbox import NotificationOutbox
        outbox = NotificationOutbox()
        return flask.jsonify({"status": "success", "counts": outbox.counts(),
                              "items": outbox.recent(5)})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/research", methods=["GET"])
def research_page():
    return flask.render_template("research.html")


@web_app.route("/system", methods=["GET"])
def system_page():
    return flask.render_template("system.html")


@web_app.route("/diagnostics", methods=["GET"])
def diagnostics_page():
    return flask.render_template("diagnostics.html")


@web_app.route("/api/indicators/save", methods=["POST"])
def api_indicator_save():
    from StockInvestmentTool.indicators.store import save_indicator

    payload = flask.request.get_json(force=True, silent=True) or {}
    try:
        result = save_indicator(
            str(payload.get("name", "")), str(payload.get("kind", "composite")),
            str(payload.get("expr", "")), str(payload.get("description", "")),
        )
        return flask.jsonify({"status": "success", "indicator": result})
    except ValueError as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        logger.exception("保存用户指标失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/indicators/toggle", methods=["POST"])
def api_indicator_toggle():
    from StockInvestmentTool.indicators.store import set_enabled

    payload = flask.request.get_json(force=True, silent=True) or {}
    try:
        result = set_enabled(str(payload.get("name", "")), bool(payload.get("enabled", True)))
        return flask.jsonify({"status": "success", **result})
    except ValueError as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/indicators/delete", methods=["POST"])
def api_indicator_delete():
    from StockInvestmentTool.indicators.store import delete_indicator

    payload = flask.request.get_json(force=True, silent=True) or {}
    try:
        name = str(payload.get("name", ""))
        return flask.jsonify({"status": "success", "name": name, "deleted": delete_indicator(name)})
    except ValueError as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/indicators/preview", methods=["POST"])
def api_indicator_preview():
    """Validate and preview a configured/ad-hoc indicator expression."""
    from StockInvestmentTool.datasource.base import WarehouseSource
    from StockInvestmentTool.indicators.engine import IndicatorRegistry

    payload = flask.request.get_json(force=True, silent=True) or {}
    code = (payload.get("code") or "").strip()
    expr = (payload.get("expr") or "").strip()
    if not code:
        return flask.jsonify({"status": "error", "error": "缺少股票代码"}), 400
    if not expr:
        return flask.jsonify({"status": "error", "error": "缺少指标表达式"}), 400
    if len(expr) > 200:
        return flask.jsonify({"status": "error", "error": "指标表达式不能超过 200 个字符"}), 400
    try:
        kline = WarehouseSource().fetch_daily_series(code, days=320)
        if kline is None or kline.empty:
            return flask.jsonify({"status": "error", "error": "没有可用于预览的天级行情数据"}), 404
        series = IndicatorRegistry().evaluate_expression(kline, expr)
        rows = []
        for date, value in zip(kline["date"].tail(180), series.tail(180)):
            rows.append({
                "date": str(date)[:10],
                "value": round(float(value), 4) if value == value else None,
            })
        valid = [r["value"] for r in rows if r["value"] is not None]
        if not valid:
            return flask.jsonify({"status": "error", "error": "指标没有产生有效值，请检查周期或表达式"}), 400
        return flask.jsonify({
            "status": "success", "code": code, "expr": expr,
            "latest": valid[-1], "series": rows,
        })
    except ValueError as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        logger.exception("指标预览失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/rules/schema", methods=["GET"])
def api_rules_schema():
    """返回规则 type 与参数 schema，供前端动态渲染表单（I1b）。"""
    from StockInvestmentTool.strategy.rule_registry import get_rule_registry
    kind = flask.request.args.get("kind", "")
    try:
        reg = get_rule_registry()
        if kind in ("buy", "sell"):
            schemas = [reg.get(kind, t).to_dict() for t in reg.types(kind)]
        else:
            schemas = reg.describe()
        return flask.jsonify({"status": "success", "rules": schemas})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/schemes/compose", methods=["POST"])
def api_schemes_compose():
    """结构化模型 → YAML 预览（I2）。"""
    from StockInvestmentTool.core import composer
    try:
        model = flask.request.get_json(force=True, silent=True) or {}
        content = composer.model_to_yaml(model)
        return flask.jsonify({"status": "success", "yaml": content})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/schemes/parse", methods=["POST"])
def api_schemes_parse():
    """既有 YAML → 结构化模型（前端编辑回填）。"""
    from StockInvestmentTool.core import composer
    try:
        content = flask.request.get_json(force=True, silent=True) or {}
        model = composer.yaml_to_model(content.get("content", ""))
        return flask.jsonify({"status": "success", "model": model})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/schemes/save", methods=["POST"])
def api_schemes_save():
    """保存方案（原子写入 + 记录版本）（I3）。"""
    from StockInvestmentTool.core import scheme_store
    from StockInvestmentTool.core.registry import SchemeRegistry
    try:
        payload = flask.request.get_json(force=True, silent=True) or {}
        name = (payload.get("name") or "").strip()
        content = payload.get("content") or ""
        if not name:
            return flask.jsonify({"status": "error", "error": "缺少方案名"}), 400
        res = scheme_store.save_scheme(name, content)
        # 重载注册中心使新方案立即生效（免重启）
        SchemeRegistry().reload()
        return flask.jsonify({"status": "success", **res})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/schemes/validate-run", methods=["POST"])
def api_schemes_validate_run():
    """Run a non-persistent sample backtest for a composed scheme."""
    from StockInvestmentTool.core.composer import yaml_to_model, model_to_config
    from StockInvestmentTool.datasource.base import WarehouseSource
    from StockInvestmentTool.backtest.engine import BacktestEngine

    payload = flask.request.get_json(force=True, silent=True) or {}
    content = payload.get("content") or ""
    code = (payload.get("code") or "").strip()
    if not content or not code:
        return flask.jsonify({"status": "error", "error": "需要方案 YAML 和股票代码"}), 400
    try:
        model = yaml_to_model(content)
        scheme = model_to_config(model)
        kline = WarehouseSource().fetch_daily_series(code, days=750)
        if kline is None or len(kline) < 80:
            return flask.jsonify({"status": "error", "error": "样本行情不足 80 个交易日"}), 400
        result = BacktestEngine(
            kline, initial_cash=scheme.backtest.initial_cash,
            stock_type=(payload.get("stock_type") or "B").strip(), scheme=scheme,
        ).run_custom(
            trail_threshold=float(payload.get("trail_threshold", 0.05)),
            offset=float(payload.get("offset", 0.0)),
        )
        detail = result.get("backtest", {})
        equity = detail.get("equity_curve") or []
        peak = float(equity[0]) if equity else 0.0
        max_drawdown = 0.0
        for value in equity:
            value = float(value)
            peak = max(peak, value)
            if peak > 0:
                max_drawdown = min(max_drawdown, value / peak - 1)
        return flask.jsonify({
            "status": "success", "scheme": scheme.name, "code": code,
            "trades": len(detail.get("trades", [])),
            "total_return": detail.get("total_return"),
            "max_drawdown": round(max_drawdown * 100, 2),
            "final_cash": detail.get("final_cash"),
            "equity_points": len(equity),
        })
    except (ValueError, KeyError, TypeError) as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        logger.exception("方案样本验证失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/schemes/validate", methods=["POST"])
def api_schemes_validate():
    """Validate and persist a user scheme's current content and sample result."""
    from StockInvestmentTool.core import scheme_store
    from StockInvestmentTool.core.composer import validate_yaml
    from StockInvestmentTool.web.app import api_schemes_validate_run
    payload = flask.request.get_json(force=True, silent=True) or {}
    content = payload.get("content") or ""
    name = (payload.get("name") or "").strip()
    if not content and name:
        try:
            content = __import__("StockInvestmentTool.portfolio.settings", fromlist=["read_scheme"]).read_scheme(name)
        except Exception as exc:
            return flask.jsonify({"status": "error", "error": str(exc)}), 400
    check = validate_yaml(content)
    if not check["ok"]:
        return flask.jsonify({"status": "error", "error": check["error"]}), 400
    try:
        model = __import__("StockInvestmentTool.core.composer", fromlist=["yaml_to_model"]).yaml_to_model(content)
        name = model["name"]
        from StockInvestmentTool.core import scheme_store
        if name in scheme_store.BUILTIN_SCHEMES:
            return flask.jsonify({"status": "error", "error": "内置方案不可进入用户发布流程"}), 400
        # Validate the current editor content without persisting it first.
        code = (payload.get("code") or "").strip()
        if not code:
            return flask.jsonify({"status": "error", "error": "静态校验通过，但还需要样本股票代码"}), 400
        with flask.current_app.test_request_context(json={**payload, "content": content}):
            result = api_schemes_validate_run()
        if isinstance(result, tuple):
            response, status = result
            if status >= 400:
                return response, status
        else:
            response = result
        summary = response.get_json()
        res = scheme_store.save_scheme(name, content)
        meta = scheme_store.set_validation(name, sample_code=code, result=summary, validated_by=os.getenv("ADMIN_USER", "admin"))
        return flask.jsonify({"status": "success", "scheme": res, "metadata": meta})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/schemes/publish", methods=["POST"])
def api_schemes_publish():
    from StockInvestmentTool.core import scheme_store
    from StockInvestmentTool.core.registry import SchemeRegistry
    try:
        name = (flask.request.get_json(force=True, silent=True) or {}).get("name", "").strip()
        if name in scheme_store.BUILTIN_SCHEMES:
            raise ValueError("内置方案不可通过用户发布流程修改")
        result = scheme_store.publish(name)
        SchemeRegistry().reload()
        return flask.jsonify({"status": "success", "metadata": result,
                              "impact": _scheme_impact(name)})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


def _scheme_impact(name: str) -> dict:
    manager = _get_manager()
    positions = manager.storage.get_positions()
    simulations = manager.storage.get_simulations()
    meta = __import__("StockInvestmentTool.core.scheme_store", fromlist=["metadata"]).metadata(name)
    return {"name": name, "open_positions": sum(p.scheme_name == name and p.status == "open" for p in positions),
            "closed_positions": sum(p.scheme_name == name and p.status == "closed" for p in positions),
            "simulations": sum(s.scheme_name == name for s in simulations), "default": meta["default"],
            "enabled": meta["enabled"], "state": meta["state"],
            "historical_positions_use_snapshot": True, "new_positions_only": True,
            "default_will_change": False}


@web_app.route("/api/schemes/impact", methods=["GET"])
def api_schemes_impact():
    try:
        name = flask.request.args.get("name", "").strip()
        return flask.jsonify({"status": "success", **_scheme_impact(name)})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 404


@web_app.route("/api/schemes/list", methods=["GET"])
def api_schemes_list():
    """列出全部方案（内置 + 用户，含启停/默认），供方案管理页（FR-2.5）。"""
    from StockInvestmentTool.core import scheme_store
    from StockInvestmentTool.core.registry import SchemeRegistry
    try:
        builtin = [
            {
                "name": s.name, "version": s.version,
                "description": s.description,
                "applicable_types": list(s.applicable_types),
                "enabled": True, "default": False, "is_builtin": True,
                "source": s.source,
            }
            for s in SchemeRegistry().list()
        ]
        user = scheme_store.list_scheme_stores()
        for item in user:
            item["default"] = scheme_store.is_default(item["name"])
        return flask.jsonify({"status": "success", "schemes": builtin + user})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/schemes/versions", methods=["GET"])
def api_schemes_versions():
    """方案版本历史（I3b）。"""
    from StockInvestmentTool.core import scheme_store
    name = flask.request.args.get("name", "")
    try:
        return flask.jsonify({"status": "success",
                              "versions": scheme_store.read_versions(name)})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/schemes/toggle", methods=["POST"])
def api_schemes_toggle():
    """方案启用/停用（I3c）。"""
    from StockInvestmentTool.core import scheme_store
    from StockInvestmentTool.core.registry import SchemeRegistry
    try:
        payload = flask.request.get_json(force=True, silent=True) or {}
        name = (payload.get("name") or "").strip()
        enabled = bool(payload.get("enabled", True))
        scheme_store.set_enabled(name, enabled)
        SchemeRegistry().reload()
        return flask.jsonify({"status": "success", "name": name, "enabled": enabled})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/schemes/default", methods=["POST"])
def api_schemes_default():
    """设置为默认方案。"""
    from StockInvestmentTool.core import scheme_store
    from StockInvestmentTool.core.registry import SchemeRegistry
    try:
        payload = flask.request.get_json(force=True, silent=True) or {}
        name = (payload.get("name") or "").strip()
        scheme_store.set_default(name)
        SchemeRegistry().reload()
        return flask.jsonify({"status": "success", "name": name})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/schemes/clone", methods=["POST"])
def api_schemes_clone():
    """从现有方案复制（FR-2.5）。"""
    from StockInvestmentTool.core import scheme_store
    from StockInvestmentTool.core.registry import SchemeRegistry
    try:
        payload = flask.request.get_json(force=True, silent=True) or {}
        src = (payload.get("src") or "").strip()
        new_name = (payload.get("new_name") or "").strip()
        if not new_name:
            return flask.jsonify({"status": "error", "error": "缺少新方案名"}), 400
        res = scheme_store.clone_scheme(src, new_name)
        SchemeRegistry().reload()
        return flask.jsonify({"status": "success", **res})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/schemes/delete", methods=["POST"])
def api_schemes_delete():
    """删除方案（内置保护）。"""
    from StockInvestmentTool.core import scheme_store
    from StockInvestmentTool.core.registry import SchemeRegistry
    try:
        payload = flask.request.get_json(force=True, silent=True) or {}
        name = (payload.get("name") or "").strip()
        scheme_store.delete_scheme(name)
        SchemeRegistry().reload()
        return flask.jsonify({"status": "success", "name": name})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/schemes/rollback", methods=["POST"])
def api_schemes_rollback():
    """回滚方案到历史版本。"""
    from StockInvestmentTool.core import scheme_store
    from StockInvestmentTool.core.registry import SchemeRegistry
    try:
        payload = flask.request.get_json(force=True, silent=True) or {}
        name = (payload.get("name") or "").strip()
        ts = float(payload.get("ts", 0))
        content = scheme_store.rollback_version(name, ts)
        if content is None:
            return flask.jsonify({"status": "error", "error": "未找到对应版本"}), 404
        res = scheme_store.save_scheme(name, content)
        SchemeRegistry().reload()
        return flask.jsonify({"status": "success", **res})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/strategy-composer", methods=["GET"])
def strategy_composer_page():
    """策略编排器页面（表单 → YAML 预览 → 保存）。"""
    return flask.render_template("strategy_composer.html", error=None)


@web_app.route("/indicator-center", methods=["GET"])
def indicator_center_page():
    """Read-only indicator catalogue with expression validation and preview."""
    return flask.render_template("indicator_center.html")


@web_app.route("/analyze", methods=["POST"])
def analyze():
    """执行分析（同步等待结果）"""
    code = flask.request.form.get("code", "").strip()
    name = flask.request.form.get("name", "").strip()
    start_date = flask.request.form.get("start_date", "")
    end_date = flask.request.form.get("end_date", "")
    do_backtest = flask.request.form.get("backtest") == "on"
    do_prompt = flask.request.form.get("prompt") == "on"
    do_api = flask.request.form.get("api") == "on"
    scheme_name = flask.request.form.get("scheme", "default_value").strip() or "default_value"
    stock_type = flask.request.form.get("stock_type", "B").strip().upper() or "B"
    if stock_type not in ("A", "B", "C", "D"):
        stock_type = "B"
    try:
        initial_cash = float(flask.request.form.get("initial_cash", "100000"))
    except (ValueError, TypeError):
        initial_cash = 100000

    if not code or not name:
        return flask.jsonify({"status": "error", "error": "请填写股票代码和名称"}), 400

    # 标准化代码
    try:
        code = StockDataFetcher.normalize_code(code)
    except Exception:
        return flask.jsonify({"status": "error", "error": "股票代码格式错误"}), 400

    # 校验方案存在
    try:
        registry = SchemeRegistry()
        if not registry.has(scheme_name):
            available = ", ".join(s.name for s in registry.list()) or "(无)"
            return flask.jsonify({"status": "error", "error": f"方案 '{scheme_name}' 不存在。可用: {available}"}), 400
    except Exception as e:
        return flask.jsonify({"status": "error", "error": f"方案加载失败: {e}"}), 500

    # 默认日期
    if not end_date:
        end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    if not start_date:
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

    # 后台执行
    task_id = f"task_{datetime.now().strftime('%H%M%S_%f')}"
    _analysis_status[task_id] = {
        "status": "running", "stage": "初始化", "progress": 0, "result": None
    }

    thread = threading.Thread(
        target=_run_analysis,
        args=(task_id, code, name, start_date, end_date,
              do_backtest, do_prompt, do_api, initial_cash, scheme_name, stock_type),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=300)  # 最多等5分钟

    result = _analysis_status.get(task_id, {})
    response = _to_json_safe(result)
    if not thread.is_alive():
        _analysis_status.pop(task_id, None)
    return flask.jsonify(response)


def _run_comparison(task_id: str, code: str, name: str,
                    start_date: str, end_date: str,
                    scheme_names: list[str], stock_type: str):
    """后台执行多方案对比"""
    status = _analysis_status[task_id]
    with _heavy_task_lock:
        with monitor_memory(f"comparison:{task_id}") as memory:
            try:
                status["stage"] = "初始化"
                status["progress"] = 5

                from StockInvestmentTool.comparison.runner import MultiSchemeRunner
                from StockInvestmentTool.comparison.report import write_report

                runner = MultiSchemeRunner(scheme_names)
                report = runner.compare(
                    code=code, name=name,
                    start_date=start_date, end_date=end_date,
                    stock_type=stock_type,
                    progress_callback=lambda stage, progress: status.update(
                        stage=stage, progress=progress
                    ),
                )

                status["stage"] = "生成报告"
                status["progress"] = 95
                report_path, chart_paths = write_report(report)

                result = report.to_dict()
                result["report_file"] = Path(report_path).name
                try:
                    result["report_content"] = Path(report_path).read_text(encoding="utf-8")
                except OSError:
                    pass
                result["charts"] = [{"name": k, "file": Path(v).name} for k, v in chart_paths.items()]

                status["result"] = result
                status["status"] = "success"
                status["stage"] = "完成"
                status["progress"] = 100
            except Exception as e:
                logger.exception("对比失败")
                status["status"] = "error"
                status["error"] = str(e)
                status["stage"] = "失败"
                status["progress"] = -1
        _attach_memory(status, "comparison", memory)


@web_app.route("/compare", methods=["GET", "POST"])
def compare():
    """方案对比"""
    if flask.request.method == "GET":
        try:
            registry = SchemeRegistry()
            schemes = registry.list()
        except Exception as e:
            logger.warning("方案加载失败: %s", e)
            schemes = []
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        last_year = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        return flask.render_template("compare.html",
                                     yesterday=yesterday,
                                     last_year=last_year,
                                     schemes=schemes)

    # POST: 执行对比
    code = flask.request.form.get("code", "").strip()
    name = flask.request.form.get("name", "").strip()
    start_date = flask.request.form.get("start_date", "")
    end_date = flask.request.form.get("end_date", "")
    stock_type = flask.request.form.get("stock_type", "B").strip().upper() or "B"
    if stock_type not in ("A", "B", "C", "D"):
        stock_type = "B"
    scheme_names = [s.strip() for s in flask.request.form.getlist("schemes") if s.strip()]

    if not code or not name:
        return flask.jsonify({"status": "error", "error": "请填写股票代码和名称"}), 400
    if len(scheme_names) < 2:
        return flask.jsonify({"status": "error", "error": "请至少选择 2 个方案进行对比"}), 400

    try:
        code = StockDataFetcher.normalize_code(code)
    except Exception:
        return flask.jsonify({"status": "error", "error": "股票代码格式错误"}), 400

    # 默认日期
    if not end_date:
        end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    if not start_date:
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

    task_id = f"cmp_{datetime.now().strftime('%H%M%S_%f')}"
    _analysis_status[task_id] = {
        "status": "running", "stage": "初始化", "progress": 0, "result": None
    }
    thread = threading.Thread(
        target=_run_comparison,
        args=(task_id, code, name, start_date, end_date, scheme_names, stock_type),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=300)

    result = _analysis_status.get(task_id, {})
    response = _to_json_safe(result)
    if not thread.is_alive():
        _analysis_status.pop(task_id, None)
    return flask.jsonify(response)


# ── 持仓管理 ────────────────────────────────────

def _get_manager():
    """获取组合管理器（延迟初始化，共享实例）"""
    from StockInvestmentTool.portfolio.manager import PortfolioManager
    mgr = getattr(flask.g, "_portfolio_manager", None)
    if mgr is None:
        mgr = PortfolioManager()
        flask.g._portfolio_manager = mgr
    return mgr


@web_app.route("/portfolio", methods=["GET"])
def portfolio_dashboard():
    """旧持仓仪表盘 → 已并入 /dashboard/warroom（持仓页）"""
    return flask.redirect("/dashboard/warroom")


@web_app.route("/api/classify", methods=["GET"])
def api_classify():
    """股票类型自动识别（建仓表单用）：行业 + 财务缓存 → classify_stock。"""
    code = flask.request.args.get("code", "").strip()
    if not code:
        return flask.jsonify({"status": "error", "error": "缺少 code"}), 400
    try:
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher
        from StockInvestmentTool.strategy.stock_classifier import classify_stock

        code = StockDataFetcher.normalize_code(code)
        # 场内 ETF/LOF → 类型 E（不走 A/B/C/D 股票分类）
        if StockDataFetcher.detect_type(code) == "etf":
            return flask.jsonify({"status": "success", "stock_type": "E",
                                  "industry": "场内ETF/LOF", "roe": 0, "rev_growth": 0})
        fetcher = StockDataFetcher()
        # 行业：优先读数据层 meta.db（采集后写入），无则实时拉+写缓存
        from StockInvestmentTool.warehouse.storage import Warehouse
        wh = Warehouse()
        code_nodot = code.replace(".", "")
        industry = wh.get_industry(code_nodot)
        if not industry:
            industry = fetcher.get_stock_industry(code)
            if industry:
                wh.update_industry(code_nodot, industry)
        # 财务史：优先读数据层 fundamentals 分区，无则实时拉
        roe = rev_growth = 0.0
        fund = wh.read_fundamentals(code_nodot)
        if fund is None or fund.empty:
            df = fetcher.get_fundamental_history(code, years=1)
            if df is not None and not df.empty:
                wh.write_fundamentals(code_nodot, df)
                fund = df
        if fund is not None and not fund.empty:
            row = fund.iloc[-1]
            roe = float(row.get("roe") or 0)
            rev_growth = float(row.get("revenue_yoy") or 0)
        stype = classify_stock(industry=industry, roe=roe,
                               revenue_growth=rev_growth, div_yield=0, pe=0)
        return flask.jsonify({"status": "success", "stock_type": stype,
                              "industry": industry, "roe": roe, "rev_growth": rev_growth})
    except Exception as e:
        logger.warning("类型识别失败 %s: %s", code, e)
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/stock/lookup", methods=["GET"])
def api_stock_lookup():
    """股票信息自动关联：输编号(可不带前缀) → 名称 + 最近价格 + 历史价格。

    Args:
        code: 股票编号（600900 / sh.600900 / sz000001 均可）
        date: 可选，查指定日期价格

    Returns:
        {code, name, asset_type, current_price, price_at_date(可选), board}
    """
    code = flask.request.args.get("code", "").strip()
    date = (flask.request.args.get("date") or "").strip()[:10]
    if not code:
        return flask.jsonify({"status": "error", "error": "缺少 code"}), 400
    try:
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher
        from StockInvestmentTool.warehouse.storage import Warehouse

        norm = StockDataFetcher.normalize_code(code)
        code_nodot = norm.replace(".", "")

        # 从 meta.db 查名称/类型/板块
        w = Warehouse()
        conn = w._conn()
        name = ""
        asset_type = "stock"
        board = ""
        try:
            row = conn.execute(
                "SELECT name, type, board FROM instruments WHERE code=?", (code_nodot,)
            ).fetchone()
            if row:
                name, asset_type, board = row[0], row[1] or "stock", row[2] or ""
        finally:
            conn.close()

        # 从 warehouse 查最近价格 + 指定日期价格
        current_price = None
        price_at_date = None
        latest = w.read_daily(w.available_months("daily")[-1]) if w.available_months("daily") else None
        if latest is not None and not latest.empty and "code" in latest.columns:
            sub = latest[latest["code"] == code_nodot]
            if len(sub):
                current_price = round(float(sub["close"].iloc[-1]), 2)
            if date:
                dsub = sub[sub["date"].astype(str).str[:10] == date]
                if len(dsub):
                    price_at_date = round(float(dsub["close"].iloc[-1]), 2)

        return flask.jsonify({"status": "success", "code": norm,
                              "name": name or code_nodot,
                              "asset_type": asset_type, "board": board,
                              "current_price": current_price,
                              "price_at_date": price_at_date})
    except Exception as e:
        logger.warning("股票查询失败 %s: %s", code, e)
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/portfolio/add", methods=["GET", "POST"])
def portfolio_add():
    """新建持仓"""
    if flask.request.method == "GET":
        from StockInvestmentTool.core.registry import SchemeRegistry
        try:
            schemes = SchemeRegistry().list()
        except Exception:
            schemes = []
        # 支持从观察池跳转预填（?code=&name=）
        prefill = {
            "code": flask.request.args.get("code", ""),
            "name": flask.request.args.get("name", ""),
            "stock_type": flask.request.args.get("stock_type", "B"),
        }
        return flask.render_template("portfolio_add.html", schemes=schemes, prefill=prefill)

    mgr = _get_manager()
    code = flask.request.form.get("code", "").strip()
    name = flask.request.form.get("name", "").strip()
    try:
        shares = float(flask.request.form.get("shares", "0"))
        cost = float(flask.request.form.get("cost", "0"))
    except (ValueError, TypeError):
        return flask.jsonify({"status": "error", "error": "份额和成本必须为数字"}), 400
    scheme_name = flask.request.form.get("scheme", "default_value").strip()
    stock_type = flask.request.form.get("stock_type", "B").strip().upper()
    buy_date = flask.request.form.get("buy_date", "") or datetime.now().strftime("%Y-%m-%d")
    notes = flask.request.form.get("notes", "").strip()

    if not code or not name:
        return flask.jsonify({"status": "error", "error": "请填写代码和名称"}), 400
    try:
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher
        code = StockDataFetcher.normalize_code(code)
        pos = mgr.add_position(code, name, shares, cost, buy_date,
                               scheme_name, stock_type, notes)
        return flask.jsonify({"status": "success", "position_id": pos.id})
    except Exception as e:
        logger.exception("建仓失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/portfolio/<int:position_id>", methods=["GET"])
def portfolio_detail(position_id):
    """持仓详情"""
    mgr = _get_manager()
    try:
        detail = mgr.get_position_detail(position_id)
        schemes = SchemeRegistry().list()
        return flask.render_template("position_detail.html", detail=detail, schemes=schemes)
    except ValueError as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 404


@web_app.route("/portfolio/<int:position_id>/transaction", methods=["POST"])
def portfolio_transaction(position_id):
    """记录交易"""
    mgr = _get_manager()
    trans_type = flask.request.form.get("type", "").strip()
    try:
        price = float(flask.request.form.get("price", "0"))
        shares = float(flask.request.form.get("shares", "0"))
        fee = float(flask.request.form.get("fee", "0") or 0)
    except (ValueError, TypeError):
        return flask.jsonify({"status": "error", "error": "价格/份额必须为数字"}), 400
    date = flask.request.form.get("date", "") or None
    reason = flask.request.form.get("reason", "").strip()

    try:
        txn = mgr.record_transaction(position_id, trans_type, price, shares, date, fee, reason)
        return flask.jsonify({"status": "success", "transaction_id": txn.id,
                              "pnl": txn.pnl})
    except ValueError as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        logger.exception("记录交易失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/portfolio/<int:position_id>/close", methods=["POST"])
def portfolio_close(position_id):
    """平仓"""
    mgr = _get_manager()
    try:
        price = float(flask.request.form.get("price", "0"))
    except (ValueError, TypeError):
        return flask.jsonify({"status": "error", "error": "价格必须为数字"}), 400
    date = flask.request.form.get("date", "") or None
    reason = flask.request.form.get("reason", "平仓").strip()
    try:
        txn = mgr.close_position(position_id, price, date, reason)
        return flask.jsonify({"status": "success", "transaction_id": txn.id, "pnl": txn.pnl})
    except ValueError as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/portfolio/<int:position_id>/scheme", methods=["POST"])
def position_change_scheme(position_id):
    """切换持仓方案并重新生成止盈止损位"""
    mgr = _get_manager()
    scheme = flask.request.form.get("scheme", "").strip()
    if not scheme:
        return flask.jsonify({"status": "error", "error": "请选择方案"}), 400
    try:
        result = mgr.change_position_scheme(position_id, scheme)
        return flask.jsonify({"status": "success", "result": result})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/portfolio/<int:position_id>/edit", methods=["POST"])
def portfolio_edit(position_id):
    """编辑/纠错持仓"""
    mgr = _get_manager()
    shares = flask.request.form.get("shares")
    avg_cost = flask.request.form.get("avg_cost")
    notes = flask.request.form.get("notes")
    try:
        p = mgr.edit_position(
            position_id,
            shares=float(shares) if shares not in (None, "") else None,
            avg_cost=float(avg_cost) if avg_cost not in (None, "") else None,
            notes=notes if notes else None,
        )
        return flask.jsonify({"status": "success", "position": p.to_dict()})
    except ValueError as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/portfolio/<int:position_id>/delete", methods=["POST"])
def portfolio_delete(position_id):
    """删除持仓（破坏性）：open 持仓还原占用资金，级联删交易/建议。

    前端必须弹窗确认后才调用。
    """
    mgr = _get_manager()
    try:
        result = mgr.delete_position(position_id)
        return flask.jsonify(result)
    except ValueError as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        logger.exception("删除持仓失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/portfolio/<int:position_id>/advice", methods=["GET"])
def portfolio_advice(position_id):
    """刷新并重新生成单个持仓建议"""
    mgr = _get_manager()
    try:
        result = mgr.refresh_position(position_id)
        return flask.jsonify({"status": "success", "result": result})
    except ValueError as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 404


@web_app.route("/portfolio/refresh", methods=["POST"])
def portfolio_refresh():
    """刷新所有持仓价格 + 建议"""
    mgr = _get_manager()
    try:
        results = mgr.refresh_all()
        return flask.jsonify({"status": "success", "results": results})
    except Exception as e:
        logger.exception("刷新失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/portfolio/export", methods=["GET"])
def portfolio_export():
    """导出 Excel"""
    try:
        from StockInvestmentTool.portfolio.export import export_to_excel
        mgr = _get_manager()
        path = export_to_excel(mgr)
        return flask.send_file(path, as_attachment=True,
                               download_name="portfolio_template.xlsx")
    except Exception as e:
        logger.exception("导出失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/portfolio/import", methods=["POST"])
def portfolio_import():
    """从 Excel 导入"""
    file = flask.request.files.get("file")
    if not file or not file.filename:
        return flask.jsonify({"status": "error", "error": "请选择 Excel 文件"}), 400
    if flask.request.content_length and flask.request.content_length > 10 * 1024 * 1024:
        return flask.jsonify({"status": "error", "error": "Excel 文件不能超过 10MB"}), 413
    if not file.filename.lower().endswith((".xlsx", ".xls")):
        return flask.jsonify({"status": "error", "error": "仅支持 Excel 文件"}), 400
    tmp = Path(Config.DATA_DIR) / f"import_{datetime.now():%Y%m%d%H%M%S}.xlsx"
    file.save(str(tmp))
    try:
        from StockInvestmentTool.portfolio.export import import_from_excel
        mgr = _get_manager()
        result = import_from_excel(mgr, tmp)
        tmp.unlink(missing_ok=True)
        return flask.jsonify({"status": "success", "result": result})
    except Exception as e:
        tmp.unlink(missing_ok=True)
        logger.exception("导入失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/watchlist", methods=["POST"])
def watchlist_add():
    """新增自选/观察（自定义加入，可填原因）"""
    mgr = _get_manager()
    code = flask.request.form.get("code", "").strip()
    name = flask.request.form.get("name", "").strip()
    added_time = (flask.request.form.get("added_time") or "").strip()[:10]
    reason = (flask.request.form.get("reason") or "").strip()
    try:
        capital = float(flask.request.form.get("target_capital", "0") or 0)
    except (ValueError, TypeError):
        capital = 0
    if not code:
        return flask.jsonify({"status": "error", "error": "请填写代码"}), 400
    try:
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher
        code = StockDataFetcher.normalize_code(code)
        item = mgr.add_watchlist(code, name or code, target_capital=capital,
                                 added_time=added_time, notes=reason, source="manual")
        return flask.jsonify({"status": "success", "watchlist_id": item.id})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/watchlist", methods=["GET"])
def watchlist_page():
    """自选页：稳定观察股 + 展开K线 + 模拟/建仓"""
    from StockInvestmentTool.portfolio.dashboard import DashboardService
    try:
        svc = DashboardService(_get_manager())
        items = [w.to_dict() for w in svc.manager.get_watchlist()]
        return flask.render_template("watchlist.html", items=items, error=None)
    except Exception as e:
        logger.exception("自选页加载失败")
        return flask.render_template("watchlist.html", items=[], error=str(e))


@web_app.route("/api/simulation", methods=["POST"])
def api_simulation():
    """跑一次模拟（分析快照），观察/自选页按钮用"""
    mgr = _get_manager()
    code = flask.request.form.get("code", "").strip()
    name = flask.request.form.get("name", "").strip()
    scheme = flask.request.form.get("scheme", "default_value").strip()
    stock_type = flask.request.form.get("stock_type", "B").strip().upper()
    if not code:
        return flask.jsonify({"status": "error", "error": "请填写代码"}), 400
    try:
        sim = mgr.simulate(code, name or code, scheme, stock_type)
        return flask.jsonify({"status": "success", "id": sim.id, "snapshot": sim.snapshot})
    except Exception as e:
        logger.exception("模拟失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/simulation", methods=["DELETE"])
def api_simulation_delete():
    mgr = _get_manager()
    try:
        sim_id = int(flask.request.form.get("id", flask.request.args.get("id", "0")))
    except (TypeError, ValueError):
        return flask.jsonify({"status": "error", "error": "缺少 id"}), 400
    mgr.delete_simulation(sim_id)
    return flask.jsonify({"status": "success"})


@web_app.route("/simulation", methods=["GET"])
def simulation_page():
    """模拟页：已模拟标的全量点位 + 建仓/重新分析/删除"""
    from StockInvestmentTool.core.registry import SchemeRegistry
    from StockInvestmentTool.portfolio.dashboard import DashboardService
    try:
        svc = DashboardService(_get_manager())
        items = [s.to_dict() for s in svc.manager.get_simulations()]
        schemes = [s.name for s in SchemeRegistry().list()]
        return flask.render_template("simulation.html", items=items,
                                     schemes=schemes, error=None)
    except Exception as e:
        logger.exception("模拟页加载失败")
        return flask.render_template("simulation.html", items=[], schemes=[],
                                     error=str(e))


@web_app.route("/strategy", methods=["GET"])
def strategy_page():
    """策略实验室页：扫描选股 + 回测验证"""
    return flask.render_template("strategy.html", error=None)


@web_app.route("/market-discovery", methods=["GET"])
def market_discovery_page():
    return flask.render_template("market_discovery.html")


@web_app.route("/api/market-discovery/stocks", methods=["POST"])
def api_market_discovery_stocks():
    try:
        from StockInvestmentTool.market_discovery.service import discover_stocks
        from StockInvestmentTool.market_discovery.storage import DiscoveryRunStore
        payload = flask.request.get_json(force=True, silent=True) or {}
        result = discover_stocks(
            payload.get("conditions"), top_n=payload.get("top_n", 50),
            as_of=(payload.get("as_of") or "")[:10],
        )
        result["run_id"] = DiscoveryRunStore().save(
            as_of=result.get("as_of"), conditions=result.get("conditions", {}),
            result_count=result.get("count", 0),
        )
        return flask.jsonify({"status": "success", **result})
    except (TypeError, ValueError) as exc:
        return flask.jsonify({"status": "error", "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("本地个股发现失败")
        return flask.jsonify({"status": "error", "error": str(exc)}), 500


@web_app.route("/api/market-discovery/series", methods=["GET"])
def api_market_discovery_series():
    try:
        from StockInvestmentTool.market_discovery.service import stock_series
        code = (flask.request.args.get("code") or "").strip().lower().replace(".", "")
        if not code:
            return flask.jsonify({"status": "error", "error": "缺少股票代码"}), 400
        return flask.jsonify({"status": "success", **stock_series(
            code, days=flask.request.args.get("days", 120),
            as_of=(flask.request.args.get("as_of") or "")[:10],
        )})
    except (TypeError, ValueError) as exc:
        return flask.jsonify({"status": "error", "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("个股发现序列读取失败")
        return flask.jsonify({"status": "error", "error": str(exc)}), 500


@web_app.route("/api/market-discovery/options", methods=["GET"])
def api_market_discovery_options():
    try:
        from StockInvestmentTool.warehouse.storage import Warehouse
        with Warehouse()._conn() as conn:
            rows = conn.execute("SELECT DISTINCT industry FROM instruments WHERE industry IS NOT NULL AND TRIM(industry) != '' ORDER BY industry").fetchall()
        return flask.jsonify({"status": "success", "industries": [row[0] for row in rows]})
    except Exception as exc:
        return flask.jsonify({"status": "error", "error": str(exc)}), 500


@web_app.route("/operation-points", methods=["GET"])
def operation_points_page():
    return flask.render_template("operation_points.html")


@web_app.route("/api/operation-points/configs", methods=["GET", "POST"])
def api_operation_points_configs():
    from StockInvestmentTool.strategy.operation_points import OperationPointConfig
    from StockInvestmentTool.strategy.operation_points_store import OperationPointStore
    store = OperationPointStore()
    try:
        if flask.request.method == "GET":
            return flask.jsonify({"status": "success", "configs": store.list()})
        payload = flask.request.get_json(force=True, silent=True) or {}
        config = OperationPointConfig.from_dict(payload.get("config") or payload)
        return flask.jsonify({"status": "success", "config": store.save(config.to_dict())})
    except (TypeError, ValueError) as exc:
        return flask.jsonify({"status": "error", "error": str(exc)}), 400


@web_app.route("/api/operation-points/analyze", methods=["POST"])
def api_operation_points_analyze():
    try:
        from StockInvestmentTool.market_discovery.service import stock_series
        from StockInvestmentTool.strategy.operation_points import OperationPointConfig, calculate
        from StockInvestmentTool.warehouse.storage import Warehouse
        payload = flask.request.get_json(force=True, silent=True) or {}
        code = (payload.get("code") or "").strip().lower().replace(".", "")
        config = OperationPointConfig.from_dict(payload.get("config"))
        warehouse = Warehouse()
        series = stock_series(code, days=750, warehouse=warehouse)
        frame = pd.DataFrame({"date": series["dates"], "open": series["open"], "close": series["close"], "high": series["high"], "low": series["low"], "volume": series["volume"], "amount": series["amount"]})
        result = calculate(frame, config)
        from StockInvestmentTool.strategy.operation_points_store import OperationPointStore
        OperationPointStore().record_run(name=config.name, version=config.version, code=code,
                                         data_as_of=result.calculation_as_of, result=result.to_dict())
        return flask.jsonify({"status": "success", "result": result.to_dict(), "series": series})
    except (TypeError, ValueError) as exc:
        return flask.jsonify({"status": "error", "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("操作点位分析失败")
        return flask.jsonify({"status": "error", "error": str(exc)}), 500


@web_app.route("/api/operation-points/backtest", methods=["POST"])
def api_operation_points_backtest():
    try:
        from StockInvestmentTool.strategy.operation_points import OperationPointConfig, run_backtest
        from StockInvestmentTool.market_discovery.service import stock_series
        payload = flask.request.get_json(force=True, silent=True) or {}
        code = (payload.get("code") or "").strip().lower().replace(".", "")
        config = OperationPointConfig.from_dict(payload.get("config"))
        series = stock_series(code, days=750)
        frame = pd.DataFrame({"date": series["dates"], "open": series["open"], "close": series["close"], "high": series["high"], "low": series["low"], "volume": series["volume"], "amount": series["amount"]})
        result = run_backtest(frame, config)
        from StockInvestmentTool.strategy.operation_points_store import OperationPointStore
        OperationPointStore().record_run(name=config.name, version=config.version, code=code,
                                         data_as_of=series["dates"][-1] if series["dates"] else None,
                                         result=result)
        return flask.jsonify({"status": "success", "result": result})
    except (TypeError, ValueError) as exc:
        return flask.jsonify({"status": "error", "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("操作点位回测失败")
        return flask.jsonify({"status": "error", "error": str(exc)}), 500


@web_app.route("/api/strategy/scan", methods=["POST"])
def api_strategy_scan():
    """策略扫描：找当前/某时点符合条件股票 + 生成折线图"""
    from StockInvestmentTool.strategy_lab import scan, plot_hits

    try:
        as_of = (flask.request.form.get("as_of") or "").strip()[:10]
        limit_up = flask.request.form.get("limit_up_10d", "1")
        deviation = flask.request.form.get("deviation_ma20_max", "0.10")
        top_n = int(flask.request.form.get("top_n", "20"))
        conditions = {
            "limit_up_10d": int(limit_up) if limit_up else 1,
            "deviation_ma20_max": float(deviation) if deviation else 0.10,
        }
        hits = scan(conditions, as_of=as_of, top_n=top_n)
        # 生成折线图到 CHART_DIR，返回 /charts/ 路径
        charts = []
        if hits:
            paths = plot_hits(conditions, as_of=as_of, hits=hits, max_plot=5)
            charts = ["/charts/" + os.path.basename(p) for p in paths]
        return flask.jsonify({"status": "success", "hits": hits,
                              "charts": charts, "count": len(hits)})
    except Exception as e:
        logger.exception("策略扫描失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/strategy/chart", methods=["POST"])
def api_strategy_chart():
    """策略图表数据：单只股票多指标序列（ECharts，指标可勾选）"""
    from StockInvestmentTool.strategy_lab import get_series

    try:
        code = (flask.request.form.get("code") or "").strip()
        as_of = (flask.request.form.get("as_of") or "").strip()[:10]
        days = int(flask.request.form.get("days", "120"))
        metrics_raw = flask.request.form.get("metrics", "")
        metrics = [m.strip() for m in metrics_raw.split(",") if m.strip()] if metrics_raw else None
        if not code:
            return flask.jsonify({"status": "error", "error": "缺少代码"}), 400
        data = get_series(code, as_of=as_of, days=days, metrics=metrics)
        return flask.jsonify({"status": "success", **data})
    except Exception as e:
        logger.exception("策略图表数据失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/strategy/backtest/curve", methods=["POST"])
def api_strategy_backtest_curve():
    """策略 vs 大盘 累计收益曲线（ECharts 对比图）"""
    from StockInvestmentTool.strategy_lab import backtest_curve

    try:
        hold_days = int(flask.request.form.get("hold_days", "10"))
        limit_up = flask.request.form.get("limit_up_10d", "1")
        deviation = flask.request.form.get("deviation_ma20_max", "0.10")
        conditions = {
            "limit_up_10d": int(limit_up) if limit_up else 1,
            "deviation_ma20_max": float(deviation) if deviation else 0.10,
        }
        data = backtest_curve(conditions, hold_days=hold_days)
        return flask.jsonify({"status": "success", **data})
    except Exception as e:
        logger.exception("策略收益曲线失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/strategy/backtest", methods=["POST"])
def api_strategy_backtest():
    """策略回测：历史选股持有N天收益 vs 全市场基准"""
    from StockInvestmentTool.strategy_lab import backtest

    try:
        hold_days = int(flask.request.form.get("hold_days", "10"))
        limit_up = flask.request.form.get("limit_up_10d", "1")
        deviation = flask.request.form.get("deviation_ma20_max", "0.10")
        conditions = {
            "limit_up_10d": int(limit_up) if limit_up else 1,
            "deviation_ma20_max": float(deviation) if deviation else 0.10,
        }
        r = backtest(conditions, hold_days=hold_days)
        if r.get("signals", 0) == 0:
            return flask.jsonify({"status": "error", "error": "无信号"}), 400
        return flask.jsonify({"status": "success", **r})
    except Exception as e:
        logger.exception("策略回测失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/watchlist/<int:item_id>", methods=["DELETE"])
def watchlist_delete(item_id):
    """删除自选"""
    try:
        mgr = _get_manager()
        mgr.delete_watchlist(item_id)
        return flask.jsonify({"status": "success"})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


# ── 每日操作日志 ────────────────────────────────────

@web_app.route("/log", methods=["GET"])
def operation_log():
    """每日操作日志：最近实际操作流水 + 每日建议对照"""
    try:
        mgr = _get_manager()
        advices = mgr.storage.list_recent_advices(limit=200)

        # 最近实际操作流水（含已平仓持仓的交易）
        recent_txns = []
        for p in mgr.storage.get_positions():
            for t in mgr.storage.get_transactions(p.id):
                if t.trans_type in ("buy", "sell", "sell_all", "dividend"):
                    recent_txns.append({**t.to_dict(),
                                        "stock_name": p.stock_name,
                                        "stock_code": p.stock_code})
        recent_txns.sort(key=lambda x: (x.get("date") or ""), reverse=True)
        recent_txns = recent_txns[:50]

        # 预取所有涉及持仓的交易，按 (position_id, 日期) 建立当日联动
        txns_by_pos: dict[int, list] = {}
        for a in advices:
            if a.position_id not in txns_by_pos:
                txns_by_pos[a.position_id] = mgr.storage.get_transactions(a.position_id)
        day_orders: dict[tuple[int, str], list] = {}
        for pos_id, txns in txns_by_pos.items():
            for t in txns:
                day_orders.setdefault((pos_id, t.date), []).append(t)

        # 按日期分组（created_at 取日期部分），日期倒序
        groups: dict[str, list] = {}
        for a in advices:
            day = a.created_at[:10] if a.created_at else ""
            groups.setdefault(day, []).append({
                "advice": a,
                "txns": day_orders.get((a.position_id, day), []),
            })
        ordered = sorted(groups.items(), key=lambda kv: kv[0], reverse=True)

        return flask.render_template("operation_log.html",
                                     groups=ordered, total=len(advices),
                                     recent_txns=recent_txns, error=None)
    except Exception as e:
        logger.exception("操作日志加载失败")
        return flask.render_template("operation_log.html",
                                     groups=[], total=0, recent_txns=[], error=str(e))


@web_app.route("/api/log/<int:position_id>", methods=["GET"])
def operation_log_api(position_id):
    """单个持仓的建议历史 JSON（供详情页/懒加载使用）"""
    try:
        mgr = _get_manager()
        history = mgr.storage.get_advice_history(position_id, limit=200)
        return flask.jsonify({"status": "success",
                              "items": [a.to_dict() for a in history]})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/note/transaction/<int:txn_id>", methods=["POST"])
def api_note_transaction(txn_id):
    """编辑单笔交易备注"""
    mgr = _get_manager()
    note = (flask.request.form.get("note") or "").strip()
    try:
        mgr.update_transaction_note(txn_id, note)
        return flask.jsonify({"status": "success"})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/note/position/<int:position_id>", methods=["POST"])
def api_note_position(position_id):
    """编辑持仓备注"""
    mgr = _get_manager()
    note = (flask.request.form.get("note") or "").strip()
    try:
        mgr.update_position_note(position_id, note)
        return flask.jsonify({"status": "success"})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/note/watchlist/<int:item_id>", methods=["POST"])
def api_note_watchlist(item_id):
    """编辑自选备注"""
    mgr = _get_manager()
    note = (flask.request.form.get("note") or "").strip()
    try:
        mgr.update_watchlist_note(item_id, note)
        return flask.jsonify({"status": "success"})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/watchlist/<int:item_id>/added_time", methods=["POST"])
def api_watchlist_added_time(item_id):
    """设置自选观察起点时间（可回看；晚于当天视为待观察）"""
    mgr = _get_manager()
    added_time = (flask.request.form.get("added_time") or "").strip()
    if not added_time:
        return flask.jsonify({"status": "error", "error": "缺少时间"}), 400
    try:
        mgr.update_watchlist_added_time(item_id, added_time[:10])
        return flask.jsonify({"status": "success"})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


# ── 快记页（手机优先）────────────────────────────

@web_app.route("/quicklog", methods=["GET"])
def quicklog_page():
    """快记页：手机优先的快速操作/笔记录入"""
    from StockInvestmentTool.core.registry import SchemeRegistry
    mgr = _get_manager()
    try:
        watchlist = [w.to_dict() for w in mgr.get_watchlist()]
        positions = [p.to_dict() for p in mgr.storage.get_open_positions()]
        schemes = [s.name for s in SchemeRegistry().list()]
        return flask.render_template("quicklog.html",
                                     watchlist=watchlist, positions=positions,
                                     schemes=schemes, error=None)
    except Exception as e:
        logger.exception("快记页加载失败")
        return flask.render_template("quicklog.html",
                                     watchlist=[], positions=[], schemes=[],
                                     error=str(e))


@web_app.route("/quicklog", methods=["POST"])
def quicklog_submit():
    """快记提交：建仓 / 加仓 / 卖出 / 分红 / 纯笔记"""
    from StockInvestmentTool.portfolio.models import (
        TXN_BUY, TXN_SELL, TXN_SELL_ALL, TXN_DIVIDEND,
    )
    mgr = _get_manager()
    action = (flask.request.form.get("action") or "").strip()
    code = (flask.request.form.get("code") or "").strip()
    name = (flask.request.form.get("name") or "").strip()
    date = (flask.request.form.get("date") or "").strip()
    note = (flask.request.form.get("note") or "").strip()
    if not code:
        return flask.jsonify({"status": "error", "error": "请填写股票代码"}), 400

    from StockInvestmentTool.datasource.fetcher import StockDataFetcher
    try:
        code = StockDataFetcher.normalize_code(code)
    except Exception:
        pass

    pos = None
    for p in mgr.storage.get_open_positions():
        if StockDataFetcher.normalize_code(p.stock_code) == StockDataFetcher.normalize_code(code):
            pos = p
            break

    def _f(key, default=0.0):
        try:
            return float(flask.request.form.get(key) or default)
        except (ValueError, TypeError):
            return default

    try:
        if action in ("open", "add"):
            price = _f("price")
            shares = _f("shares")
            if price <= 0 or shares <= 0:
                return flask.jsonify({"status": "error", "error": "价格/数量需为正"}), 400
            if pos is None:
                scheme = (flask.request.form.get("scheme") or "default_value").strip()
                mgr.add_position(stock_code=code, stock_name=name or code,
                                 shares=shares, cost=price,
                                 buy_date=date or "2026-01-01",
                                 scheme_name=scheme, notes=note)
            else:
                mgr.record_transaction(pos.id, TXN_BUY, price=price, shares=shares,
                                       date=date or None, reason=note or "快记")
        elif action == "sell":
            if pos is None:
                return flask.jsonify({"status": "error", "error": "该股无持仓，无法卖出"}), 400
            price = _f("price")
            shares = _f("shares")
            ttype = TXN_SELL_ALL if shares <= 0 else TXN_SELL
            mgr.record_transaction(pos.id, ttype, price=price, shares=shares,
                                   date=date or None, reason=note)
        elif action == "dividend":
            if pos is None:
                return flask.jsonify({"status": "error", "error": "该股无持仓，无法记分红"}), 400
            amount = _f("amount")
            mgr.record_transaction(pos.id, TXN_DIVIDEND, price=amount, shares=0,
                                   date=date or None, reason=note)
        elif action == "watch":
            mgr.add_watchlist(code, name or code, notes=note,
                              added_time=(date or "")[:10])
        elif action == "note":
            if pos is not None:
                mgr.update_position_note(pos.id, note)
            else:
                wl = None
                for w in mgr.get_watchlist():
                    if StockDataFetcher.normalize_code(w.stock_code) == StockDataFetcher.normalize_code(code):
                        wl = w
                        break
                if wl is not None:
                    mgr.update_watchlist_note(wl.id, note)
                else:
                    mgr.add_watchlist(code, name or code, notes=note)
        else:
            return flask.jsonify({"status": "error", "error": f"未知操作: {action}"}), 400
        return flask.jsonify({"status": "success"})
    except Exception as e:
        logger.exception("快记失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 400


# ── 收益分析（累计收益率/金额 + 折线图 + 导出）────────

@web_app.route("/api/returns/chart", methods=["POST"])
def api_returns_chart():
    """为单只标的生成累计收益率折线图，返回 /charts/<filename>。"""
    from StockInvestmentTool.analysis.returns import compute_returns, build_chart
    from StockInvestmentTool.portfolio.monitor import PriceMonitor

    mgr = _get_manager()
    code = (flask.request.form.get("code") or "").strip()
    kind = (flask.request.form.get("kind") or "position").strip()
    start = (flask.request.form.get("start_date") or "").strip()
    if not code:
        return flask.jsonify({"status": "error", "error": "缺少代码"}), 400

    try:
        monitor = PriceMonitor()
        kline, _ = monitor.fetch_context_data(code)
        cost_price = None
        shares = 0.0
        title = code
        if kind == "position":
            pos = None
            from StockInvestmentTool.datasource.fetcher import StockDataFetcher
            norm = StockDataFetcher.normalize_code(code)
            for p in mgr.storage.get_open_positions():
                if StockDataFetcher.normalize_code(p.stock_code) == norm:
                    pos = p
                    break
            if pos is None:
                return flask.jsonify({"status": "error", "error": "无该持仓"}), 400
            cb = mgr.cost_basis(pos.id)
            cost_price, shares = cb["cost_price"], cb["shares"]
            start = start or pos.buy_date
            title = f"{pos.stock_name} 累计收益率（成本 {cost_price:.3f}）"
        else:
            # 未指定观察起点时用全量数据首日为起点（默认展示全历史收益）
            start = start or ""
            title = f"{code} 自观察起点收益"

        r = compute_returns(kline, start_date=start,
                            cost_price=cost_price, shares=shares)
        if r is None or r.empty:
            return flask.jsonify({"status": "error", "error": "区间无数据"}), 400
        filename = build_chart(r, title)
        if not filename:
            return flask.jsonify({"status": "error", "error": "图表生成失败"}), 500
        import os
        rel = os.path.basename(filename)
        # 额外返回 ECharts 交互数据（dates + 收益序列），前端优先用交互式渲染
        dates = [str(d)[:10] for d in r["date"]]
        ret_series = [round(float(x), 2) if x == x else None for x in r["ret_pct"]]
        close_series = [round(float(x), 2) if x == x else None for x in r["close"]]
        return flask.jsonify({"status": "success",
                              "chart": "/charts/" + rel,
                              "ret_pct": round(float(r["ret_pct"].iloc[-1]), 2),
                              "ret_amount": round(float(r["ret_amount"].iloc[-1]), 2),
                              "latest_close": round(float(r["close"].iloc[-1]), 2),
                              "chart_data": {"dates": dates, "title": title,
                                             "series": [
                                                 {"name": "累计收益%", "key": "ret_pct", "data": ret_series},
                                                 {"name": "收盘价", "key": "close", "data": close_series},
                                             ]}})
    except Exception as e:
        logger.exception("收益图生成失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/chart/stock", methods=["POST"])
def api_stock_chart_series():
    """单只股票可视化序列：日/周/月 + 指标 + 收益率双线（ECharts）"""
    from StockInvestmentTool.portfolio.dashboard import DashboardService

    try:
        code = (flask.request.form.get("code") or "").strip()
        period = (flask.request.form.get("period") or "day").strip()
        days = int(flask.request.form.get("days", "120"))
        cost = flask.request.form.get("cost_price", "")
        cost_price = float(cost) if cost else None
        metrics_raw = flask.request.form.get("metrics", "")
        metrics = [m.strip() for m in metrics_raw.split(",") if m.strip()] if metrics_raw else None
        if not code:
            return flask.jsonify({"status": "error", "error": "缺少代码"}), 400
        svc = DashboardService(_get_manager())
        data = svc.stock_chart_series(code, period=period, days=days,
                                      cost_price=cost_price, metrics=metrics)
        return flask.jsonify({"status": "success", **data})
    except Exception as e:
        logger.exception("股票图表序列失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/stock/detail", methods=["POST"])
def api_stock_detail():
    """统一个股详情（观察/自选/持仓共用）：基础指标 + 天级/盘中双视图 + 收益。

    Args:
        code: 股票代码
        kind: watch / position
        entry_date / entry_price: 自选模拟收益入场点（可选）
    """
    from StockInvestmentTool.portfolio.dashboard import DashboardService

    try:
        code = (flask.request.form.get("code") or "").strip()
        kind = (flask.request.form.get("kind") or "watch").strip()
        if not code:
            return flask.jsonify({"status": "error", "error": "缺少代码"}), 400
        entry = None
        ed = (flask.request.form.get("entry_date") or "").strip()
        ep = (flask.request.form.get("entry_price") or "").strip()
        if ed or ep:
            entry = {}
            if ed:
                entry["date"] = ed[:10]
            if ep:
                try:
                    entry["price"] = float(ep)
                except (ValueError, TypeError):
                    pass
        data = DashboardService(_get_manager()).stock_detail(code, kind=kind, entry=entry)
        return flask.jsonify({"status": "success", **data})
    except Exception as e:
        logger.exception("个股详情失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/watchlist/<int:item_id>/sim_entry", methods=["POST"])
def api_watchlist_sim_entry(item_id):
    """设置自选模拟收益入场点（日期 + 价格，可单独更新一项）。"""
    mgr = _get_manager()
    ed = (flask.request.form.get("entry_date") or "").strip()
    ep = (flask.request.form.get("entry_price") or "").strip()
    try:
        price = float(ep) if ep else 0.0
    except (ValueError, TypeError):
        return flask.jsonify({"status": "error", "error": "价格必须为数字"}), 400
    try:
        mgr.set_watchlist_sim_entry(item_id, entry_date=ed[:10] if ed else "", entry_price=price)
        return flask.jsonify({"status": "success"})
    except Exception as e:
        logger.exception("设置模拟入场点失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/returns/export", methods=["GET"])
def returns_export():
    """导出全部持仓/自选的收益明细 + 汇总到 xlsx。"""
    from StockInvestmentTool.analysis.returns import export_returns_xlsx
    mgr = _get_manager()
    try:
        entries = mgr.return_analysis()
        path = export_returns_xlsx(entries)
        return flask.send_file(path, as_attachment=True,
                               download_name=Path(path).name)
    except Exception as e:
        logger.exception("收益导出失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/morning-report", methods=["GET", "POST"])
def morning_report():
    """晨报生成/查看"""
    if flask.request.method == "POST":
        try:
            from StockInvestmentTool.portfolio.reporter import MorningReporter
            mgr = _get_manager()
            path = MorningReporter(mgr).generate(refresh=True)
            return flask.jsonify({"status": "success", "path": path})
        except Exception as e:
            logger.exception("晨报生成失败")
            return flask.jsonify({"status": "error", "error": str(e)}), 500

    # GET: 查看今日晨报
    today = f"晨报_{datetime.now():%Y-%m-%d}.md"
    path = Config.REPORT_DIR / today
    if path.exists():
        content = path.read_text(encoding="utf-8")
        return flask.render_template("morning_report.html", content=content, today=today)
    return flask.render_template("morning_report.html", content=None, today=today)


@web_app.route("/dashboard/observe", methods=["GET"])
def dashboard_observe():
    """看板页一：观察池（战前侦察）"""
    from StockInvestmentTool.portfolio.dashboard import DashboardService
    force = flask.request.args.get("refresh") == "1"
    try:
        rows = DashboardService(_get_manager()).observe_pool(use_cache=not force)
        return flask.render_template("observe.html", rows=rows, error=None,
                                     data_time=datetime.now().strftime("%Y-%m-%d %H:%M"))
    except Exception as e:
        logger.exception("观察池加载失败")
        return flask.render_template("observe.html", rows=[], error=str(e),
                                     data_time=datetime.now().strftime("%Y-%m-%d %H:%M"))


@web_app.route("/dashboard/warroom", methods=["GET"])
def dashboard_warroom():
    """持仓页：账户总览 + 每只持仓指令与点位"""
    from StockInvestmentTool.portfolio.dashboard import DashboardService
    try:
        mgr = _get_manager()
        mgr.sync_holdings_to_watchlist()  # 持仓自动进自选（去重，幂等）
        data = DashboardService(mgr).war_room()
        return flask.render_template("warroom.html", data=data, error=None)
    except Exception as e:
        logger.exception("持仓页加载失败")
        return flask.render_template("warroom.html", data=None, error=str(e))


@web_app.route("/dashboard/review", methods=["GET"])
def dashboard_review():
    """看板页三：复盘底账（战后复盘）"""
    from StockInvestmentTool.portfolio.dashboard import DashboardService
    try:
        mgr = _get_manager()
        data = DashboardService(mgr).review_ledger()
        positions = [{"id": p.id, "stock_name": p.stock_name, "stock_code": p.stock_code}
                     for p in mgr.storage.get_open_positions()]
        return flask.render_template("review.html", data=data, positions=positions, error=None)
    except Exception as e:
        logger.exception("复盘底账加载失败")
        return flask.render_template("review.html", data=None, positions=[], error=str(e))


@web_app.route("/settings", methods=["GET"])
def settings_page():
    """管理页：策略 / 初筛规则 / 通知配置"""
    from StockInvestmentTool.portfolio import settings as s

    try:
        schemes = s.list_schemes()
        screen_rules = s.read_screen_rules()
        notify_rules = s.read_notify_rules()
        notify_settings = s.read_notify_settings()
        webhook = s.webhook_status()
        from StockInvestmentTool.web.scheduler import scheduler_status
        sched = scheduler_status(flask.current_app)
        scheduler_status_str = (f"每日任务已启用 · 下次运行 {sched['next_run']}（{sched['spec']}）"
                                if sched["enabled"] else "每日任务未启用（DISABLE_SCHEDULER=1 或未装 APScheduler）")
        return flask.render_template("settings.html", schemes=schemes,
                                     screen_rules=screen_rules, notify_rules=notify_rules,
                                     notify_settings=notify_settings, webhook=webhook,
                                     scheduler_status=scheduler_status_str,
                                     error=None, ok=None)
    except Exception as e:
        logger.exception("设置页加载失败")
        return flask.render_template("settings.html", schemes=[], screen_rules="",
                                     notify_rules="", notify_settings="", webhook={}, scheduler_status="",
                                     error=str(e), ok=None)


@web_app.route("/settings/scheme", methods=["POST"])
def settings_save_scheme():
    from StockInvestmentTool.portfolio import settings as s
    name = (flask.request.form.get("name") or "").strip()
    content = flask.request.form.get("content") or ""
    try:
        s.save_scheme(name, content)
        return flask.jsonify({"status": "success", "saved": name})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/settings/screen-rules", methods=["POST"])
def settings_save_screen_rules():
    from StockInvestmentTool.portfolio import settings as s
    content = flask.request.form.get("content") or ""
    try:
        s.save_screen_rules(content)
        return flask.jsonify({"status": "success"})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/settings/notify", methods=["POST"])
def settings_save_notify():
    from StockInvestmentTool.portfolio import settings as s
    action = flask.request.form.get("action", "save")
    if action == "rules":
        content = flask.request.form.get("content") or ""
        try:
            s.save_notify_rules(content)
            return flask.jsonify({"status": "success"})
        except Exception as e:
            return flask.jsonify({"status": "error", "error": str(e)}), 400
    if action == "notify_settings":
        content = flask.request.form.get("content") or ""
        try:
            s.save_notify_settings(content)
            return flask.jsonify({"status": "success"})
        except Exception as e:
            return flask.jsonify({"status": "error", "error": str(e)}), 400
    # webhook 保存
    channel = flask.request.form.get("channel", "feishu")
    feishu_url = flask.request.form.get("feishu_url", "").strip()
    wecom_url = flask.request.form.get("wecom_url", "").strip()
    try:
        status = s.save_webhook(channel, feishu_url, wecom_url)
        return flask.jsonify({"status": "success", "webhook": status})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


# ── FR-3 通知编排器 API ────────────────────────────────────

@web_app.route("/api/notify/rules", methods=["GET", "POST"])
def api_notify_rules():
    """触发器 CRUD（I4）：GET 列表 / POST 保存。"""
    from StockInvestmentTool.notifier import triggers
    if flask.request.method == "GET":
        try:
            return flask.jsonify({"status": "success",
                                  "rules": triggers.load_triggers(),
                                  "schedule_modes": triggers.SCHEDULE_MODES})
        except Exception as e:
            return flask.jsonify({"status": "error", "error": str(e)}), 500
    # POST 保存
    try:
        payload = flask.request.get_json(force=True, silent=True) or {}
        rules = payload.get("rules") or []
        triggers.save_triggers(rules)
        # 免重启：重挂定时任务（见 init_scheduler 读取最新配置）
        try:
            from StockInvestmentTool.web.scheduler import _reload_scheduler_jobs
            _reload_scheduler_jobs(flask.current_app)
        except Exception as e:
            logger.warning("通知触发器重挂失败: %s", e)
        return flask.jsonify({"status": "success", "count": len(rules)})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/notify/config", methods=["GET"])
def api_notify_config():
    """渠道 + 邮件收件人配置状态（I5 相关，不回显明文）。"""
    from StockInvestmentTool.portfolio import settings as s
    from StockInvestmentTool.notifier import triggers
    try:
        webhook = s.webhook_status()
        mail = triggers.mail_config_status()
        return flask.jsonify({"status": "success", "webhook": webhook, "mail": mail})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/notify/outbox", methods=["GET"])
def api_notify_outbox():
    """Notification delivery ledger for the notification center."""
    from StockInvestmentTool.notifier.outbox import NotificationOutbox

    try:
        outbox = NotificationOutbox()
        items = outbox.recent(200)
        for key in ("topic", "channel", "status"):
            value = flask.request.args.get(key)
            if value:
                items = [item for item in items if item.get("channel") == value or
                         (item.get("payload") or {}).get("topic") == value or item.get("status") == value]
        return flask.jsonify({"status": "success", "counts": outbox.counts(), "items": items[:50]})
    except Exception as e:
        logger.exception("通知投递台账读取失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/api/notify/mail", methods=["POST"])
def api_notify_mail():
    """保存邮件收件人 + SMTP 配置（I5，补全 email.to）。"""
    from StockInvestmentTool.notifier import triggers
    try:
        payload = flask.request.get_json(force=True, silent=True) or {}
        to = payload.get("to") or []
        if isinstance(to, str):
            to = [x.strip() for x in to.split(",") if x.strip()]
        triggers.set_email_recipients(to)
        return flask.jsonify({"status": "success", "to": to})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    """测试发送（I5）：按渠道 + 样例条件发一条测试通知。"""
    try:
        payload = flask.request.get_json(force=True, silent=True) or {}
        channel = payload.get("channel", "feishu")
        to = payload.get("to") or ""
        from StockInvestmentTool.notifier.core import (
            Digest, NotificationFragment, TOPIC_SUMMARY, live_send_digest,
        )
        digest = Digest(
            sections=[{"topic": TOPIC_SUMMARY, "title": "📡 测试通知",
                       "lines": [f"这是一条来自 StockInvestmentTool 的测试消息（渠道 {channel}）。"]}],
            meta={"subject": " 📡 测试通知"},
        )
        result = live_send_digest(digest, channel, url=payload.get("url") or "",
                                  subject="📡 测试通知", to=to or None)
        return flask.jsonify({"status": "success", "result": result})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


@web_app.route("/api/notify/outbox/<int:item_id>/retry", methods=["POST"])
def api_notify_outbox_retry(item_id):
    from StockInvestmentTool.notifier.outbox import NotificationOutbox
    if not NotificationOutbox().retry(item_id):
        return flask.jsonify({"status": "error", "error": "通知不存在或当前状态不可重试"}), 404
    return flask.jsonify({"status": "success", "id": item_id, "state": "pending"})


@web_app.route("/api/notify/outbox/retry-dead", methods=["POST"])
def api_notify_outbox_retry_dead():
    from StockInvestmentTool.notifier.outbox import NotificationOutbox
    outbox = NotificationOutbox()
    ids = [item["id"] for item in outbox.recent(200) if item.get("status") == "dead"]
    retried = [item_id for item_id in ids if outbox.retry(item_id)]
    return flask.jsonify({"status": "success", "retried": retried})


@web_app.route("/api/system/alerts", methods=["POST"])
def api_system_alerts():
    from StockInvestmentTool.ops.freshness import data_status
    from StockInvestmentTool.notifier.system_alerts import enqueue_alerts
    ids = enqueue_alerts(data_status())
    return flask.jsonify({"status": "success", "outbox_ids": ids})


@web_app.route("/notify-center", methods=["GET"])
def notify_center_page():
    """通知编排器页面（触发器/时间频率/渠道/邮箱可视化配置）。"""
    return flask.render_template("notify_composer.html", error=None)


@web_app.route("/market", methods=["GET"])
def market_page():
    """大盘页：指数 / 板块 / 持仓折线"""
    from StockInvestmentTool.portfolio.dashboard import DashboardService
    try:
        svc = DashboardService(_get_manager())
        indices = {"沪深300": "sh.000300", "上证指数": "sh.000001",
                   "深证成指": "sz.399001", "创业板指": "sz.399006"}
        board_names = svc.board_names()
        positions_codes = [{"code": p["stock_code"], "name": p["stock_name"]}
                           for p in svc.war_room()["positions"]]
        return flask.render_template("market.html", indices=indices,
                                     board_names=board_names,
                                     positions_codes=positions_codes, error=None)
    except Exception as e:
        logger.exception("大盘页加载失败")
        return flask.render_template("market.html", indices={}, board_names=[],
                                     positions_codes=[], error=str(e))


@web_app.route("/market/index_kline", methods=["GET"])
def market_index_kline():
    from StockInvestmentTool.portfolio.dashboard import DashboardService
    codes = [c for c in flask.request.args.get("codes", "").split(",") if c]
    if not codes:
        return flask.jsonify({"status": "error", "error": "缺少 codes"}), 400
    data = DashboardService(_get_manager()).index_kline(codes)
    return flask.jsonify({"status": "success", **data})


@web_app.route("/market/board_kline", methods=["GET"])
def market_board_kline():
    from StockInvestmentTool.portfolio.dashboard import DashboardService
    name = flask.request.args.get("name", "")
    if not name:
        return flask.jsonify({"status": "error", "error": "缺少 name"}), 400
    data = DashboardService(_get_manager()).board_index_kline(name)
    return flask.jsonify({"status": "success", **data})


@web_app.route("/market/stock_chart", methods=["GET"])
def market_stock_chart():
    from StockInvestmentTool.portfolio.dashboard import DashboardService
    code = flask.request.args.get("code", "")
    if not code:
        return flask.jsonify({"status": "error", "error": "缺少 code"}), 400
    data = DashboardService(_get_manager()).stock_chart(code)
    return flask.jsonify({"status": "success", **data})


@web_app.route("/dashboard/review/export")
def review_export():
    """导出复盘（统计看板 + 交易流水 Excel）"""
    from StockInvestmentTool.portfolio.dashboard import DashboardService
    from StockInvestmentTool.portfolio.export import review_to_excel
    try:
        data = DashboardService(_get_manager()).review_ledger()
        path = review_to_excel(data)
        filename = f"复盘_{datetime.now():%Y%m%d}.xlsx"
        return flask.send_file(path, as_attachment=True, download_name=filename)
    except Exception as e:
        logger.exception("复盘导出失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


@web_app.route("/settings/account", methods=["POST"])
def settings_account():
    """设置初始本金（现金余额）"""
    mgr = _get_manager()
    try:
        cash = float(flask.request.form.get("cash", ""))
    except (ValueError, TypeError):
        return flask.jsonify({"status": "error", "error": "本金必须为数字"}), 400
    if cash < 0:
        return flask.jsonify({"status": "error", "error": "本金不能为负"}), 400
    mgr.storage.set_cash(cash)
    return flask.jsonify({"status": "success", "cash": cash})


@web_app.route("/settings/reset", methods=["POST"])
def settings_reset():
    """清空数据（scope: all/positions/watchlist/simulations）"""
    mgr = _get_manager()
    scope = flask.request.form.get("scope", "all")
    keep_cash = flask.request.form.get("keep_cash") == "1"
    if scope not in ("all", "positions", "watchlist", "simulations"):
        return flask.jsonify({"status": "error", "error": "未知重置范围"}), 400
    deleted = mgr.storage.reset_data(scope=scope, keep_cash=keep_cash)
    return flask.jsonify({"status": "success", "deleted": deleted})


# ── 登录认证（.env 配置 ADMIN_PASSWORD 后启用；未配置则开发模式免登录）──

def _auth_enabled() -> bool:
    # Temporary operator-controlled bypass. Keep ADMIN_PASSWORD configured so
    # removing STOCK_DISABLE_AUTH=1 restores the normal login flow.
    return bool(os.getenv("ADMIN_PASSWORD")) and os.getenv("STOCK_DISABLE_AUTH") != "1"


def _validate_runtime_security(*, allow_test: bool = False) -> None:
    """Fail closed unless an explicit development mode is requested."""
    if os.getenv("STOCK_DEV_MODE") == "1" or os.getenv("STOCK_DISABLE_AUTH") == "1" or (allow_test and os.getenv("PYTEST_CURRENT_TEST")):
        return
    if not os.getenv("ADMIN_PASSWORD"):
        raise RuntimeError("生产模式必须配置 ADMIN_PASSWORD；调试环境请显式设置 STOCK_DEV_MODE=1")
    secret = os.getenv("SECRET_KEY", "")
    if len(secret) < 32:
        logger.warning("SECRET_KEY 长度不足 32 个字符，请尽快轮换；暂不阻止已有生产配置启动")


def _is_authed() -> bool:
    return bool(flask.session.get("admin"))


@web_app.route("/login", methods=["GET", "POST"])
def login():
    """登录页（.env 配 ADMIN_USER/ADMIN_PASSWORD 后启用）"""
    if not _auth_enabled():
        flask.session["admin"] = True
        return flask.redirect("/")
    if flask.request.method == "POST":
        user = flask.request.form.get("username", "")
        pw = flask.request.form.get("password", "")
        if user == os.getenv("ADMIN_USER", "admin") and pw == os.getenv("ADMIN_PASSWORD"):
            flask.session.clear()
            flask.session["admin"] = True
            next_path = flask.request.args.get("next") or "/"
            if not next_path.startswith("/") or next_path.startswith("//"):
                next_path = "/"
            return flask.redirect(next_path)
        return flask.render_template("login.html", error="用户名或密码错误")
    return flask.render_template("login.html", error=None)


@web_app.route("/logout")
def logout():
    flask.session.pop("admin", None)
    return flask.redirect("/login")


@web_app.before_request
def require_login():
    """未登录拦截：页面跳登录，API 返回 401；静态资源放行。"""
    endpoint = flask.request.endpoint or ""
    if endpoint in ("stock_web.login", "stock_web.serve_report", "stock_web.serve_chart"):
        return
    if flask.request.method in ("POST", "PUT", "PATCH", "DELETE") and not _same_origin_request():
        return flask.jsonify({"status": "error", "error": "跨站请求被拒绝"}), 403
    if not _auth_enabled() or _is_authed():
        return
    if flask.request.path.startswith("/api/"):
        return flask.jsonify({"status": "error", "error": "未登录"}), 401
    return flask.redirect(flask.url_for("stock_web.login", next=flask.request.path))


@web_app.before_request
def record_request_memory_start():
    """Record a cheap request baseline for post-request leak diagnostics."""
    flask.g.memory_start = memory_snapshot()


@web_app.after_request
def log_request_memory(response):
    """Log request RSS delta so heavy dashboard/data endpoints are traceable."""
    start = getattr(flask.g, "memory_start", None)
    end = memory_snapshot()
    if start:
        delta = round((end.get("rss_bytes") or 0) / 1024 / 1024
                      - (start.get("rss_bytes") or 0) / 1024 / 1024, 1)
        logger.info("memory request=%s method=%s status=%s start=%sMiB end=%sMiB delta=%sMiB",
                    flask.request.path, flask.request.method, response.status_code,
                    start.get("rss_mb"), end.get("rss_mb"), delta)
    return response


def _same_origin_request() -> bool:
    """Reject explicitly cross-origin browser mutations without breaking CLI calls."""
    origin = flask.request.headers.get("Origin")
    referer = flask.request.headers.get("Referer")
    candidate = origin or referer
    if not candidate:
        return True
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    return parsed.netloc.lower() == flask.request.host.lower()


@web_app.route("/api/daily/run", methods=["POST"])
def daily_run_now():
    """手动触发每日自动任务（后台线程执行）"""
    import threading

    def _worker():
        try:
            from StockInvestmentTool.web.scheduler import run_daily_tasks
            run_daily_tasks()
        except Exception:
            logger.exception("手动每日任务失败")

    threading.Thread(target=_worker, daemon=True).start()
    return flask.jsonify({"status": "success", "msg": "每日任务已启动（后台执行，查看日志）"})


@web_app.route("/reports/<path:filename>")
def serve_report(filename):
    """提供报告文件"""
    report_dir = Path(Config.REPORT_DIR)
    return flask.send_from_directory(str(report_dir), filename)


@web_app.route("/charts/<path:filename>")
def serve_chart(filename):
    """提供图表图片文件"""
    chart_dir = Path(Config.CHART_DIR)
    return flask.send_from_directory(str(chart_dir), filename)


def create_app():
    """创建 Flask 应用"""
    app = flask.Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
    )
    _validate_runtime_security(allow_test=True)
    app.secret_key = os.getenv("SECRET_KEY") or "stock-invest-tool-dev-secret"
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    app.register_blueprint(web_app, url_prefix="/")

    # 操作日志: 规则检查值可读格式化
    def _fmt_advice_value(v):
        if isinstance(v, bool):
            return "✅ 触发" if v else "— 未触发"
        if isinstance(v, float):
            return f"{v:.2f}"
        if isinstance(v, (list, tuple)):
            return "、".join(str(x) for x in v)
        return str(v)
    app.jinja_env.filters["fmt"] = _fmt_advice_value

    # 服务器关闭时登出 baostock
    import atexit
    atexit.register(StockDataFetcher.shutdown)

    # 每日自动任务（APScheduler，时间 DAILY_RUN_TIME 可配置）
    from StockInvestmentTool.web.scheduler import init_scheduler
    init_scheduler(app)

    return app


def main():
    """启动 Web 服务器"""
    import webbrowser

    # Windows 控制台默认 GBK，先强制 UTF-8 输出，避免 emoji 打印崩溃
    try:
        import sys as _sys
        if _sys.stdout and hasattr(_sys.stdout, "reconfigure"):
            _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    app = create_app()
    # 默认 9000：8090 落在 Windows 保留端口区间(8062-8161)内会报 WinError 10013
    port = int(os.getenv("STOCK_WEB_PORT", "9000"))
    url = f"http://127.0.0.1:{port}"

    print(f"🚀 StockInvestmentTool Web 前端启动中...")
    print(f"   打开浏览器访问: {url}")
    print(f"   按 Ctrl+C 停止服务器")

    # 自动打开浏览器
    try:
        webbrowser.open(url)
    except Exception:
        pass

    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
