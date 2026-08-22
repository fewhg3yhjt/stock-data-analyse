"""StockInvestmentTool Web 前端 — Flask 应用

使用统一 AnalysisEngine，与 CLI 共享分析管线。
"""

import json
import logging
import os
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

import flask
import numpy as np

# 确保包路径可访问
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from StockInvestmentTool.config import Config
from StockInvestmentTool.core.engine import AnalysisEngine, AnalysisOptions
from StockInvestmentTool.core.registry import SchemeRegistry
from StockInvestmentTool.datasource.fetcher import StockDataFetcher
from StockInvestmentTool.prompt.llm_client import DeepSeekClient, LLMError

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


def _run_analysis(task_id: str, code: str, name: str,
                  start_date: str, end_date: str,
                  do_backtest: bool, do_prompt: bool, do_api: bool,
                  initial_cash: float = 100000,
                  scheme_name: str = "default_value",
                  stock_type: str = "B"):
    """后台执行分析流程（通过统一 AnalysisEngine）"""
    status = _analysis_status[task_id]
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


@web_app.route("/", methods=["GET"])
def index():
    """主页：分析表单"""
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
    return flask.jsonify(_to_json_safe(result))


def _run_comparison(task_id: str, code: str, name: str,
                    start_date: str, end_date: str,
                    scheme_names: list[str], stock_type: str):
    """后台执行多方案对比"""
    status = _analysis_status[task_id]
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
    return flask.jsonify(_to_json_safe(result))


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
        industry = fetcher.get_stock_industry(code)
        roe = rev_growth = 0.0
        try:
            df = fetcher.get_fundamental_history(code, years=1)
            if df is not None and not df.empty:
                row = df.iloc[-1]
                roe = float(row.get("roe") or 0)
                rev_growth = float(row.get("revenue_yoy") or 0)
        except Exception:
            pass
        stype = classify_stock(industry=industry, roe=roe,
                               revenue_growth=rev_growth, div_yield=0, pe=0)
        return flask.jsonify({"status": "success", "stock_type": stype,
                              "industry": industry, "roe": roe, "rev_growth": rev_growth})
    except Exception as e:
        logger.warning("类型识别失败 %s: %s", code, e)
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
    """新增自选"""
    mgr = _get_manager()
    code = flask.request.form.get("code", "").strip()
    name = flask.request.form.get("name", "").strip()
    added_time = (flask.request.form.get("added_time") or "").strip()[:10]
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
                                 added_time=added_time)
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
            start = start or datetime.now().strftime("%Y-%m-%d")
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
        return flask.jsonify({"status": "success",
                              "chart": "/charts/" + rel,
                              "ret_pct": round(float(r["ret_pct"].iloc[-1]), 2),
                              "ret_amount": round(float(r["ret_amount"].iloc[-1]), 2),
                              "latest_close": round(float(r["close"].iloc[-1]), 2)})
    except Exception as e:
        logger.exception("收益图生成失败")
        return flask.jsonify({"status": "error", "error": str(e)}), 500


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
        webhook = s.webhook_status()
        from StockInvestmentTool.web.scheduler import scheduler_status
        sched = scheduler_status(flask.current_app)
        scheduler_status_str = (f"每日任务已启用 · 下次运行 {sched['next_run']}（{sched['spec']}）"
                                if sched["enabled"] else "每日任务未启用（DISABLE_SCHEDULER=1 或未装 APScheduler）")
        return flask.render_template("settings.html", schemes=schemes,
                                     screen_rules=screen_rules, notify_rules=notify_rules,
                                     webhook=webhook, scheduler_status=scheduler_status_str,
                                     error=None, ok=None)
    except Exception as e:
        logger.exception("设置页加载失败")
        return flask.render_template("settings.html", schemes=[], screen_rules="",
                                     notify_rules="", webhook={}, scheduler_status="",
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
    # webhook 保存
    channel = flask.request.form.get("channel", "feishu")
    feishu_url = flask.request.form.get("feishu_url", "").strip()
    wecom_url = flask.request.form.get("wecom_url", "").strip()
    try:
        status = s.save_webhook(channel, feishu_url, wecom_url)
        return flask.jsonify({"status": "success", "webhook": status})
    except Exception as e:
        return flask.jsonify({"status": "error", "error": str(e)}), 400


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
    return bool(os.getenv("ADMIN_PASSWORD"))


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
            flask.session["admin"] = True
            return flask.redirect(flask.request.args.get("next") or "/")
        return flask.render_template("login.html", error="用户名或密码错误")
    return flask.render_template("login.html", error=None)


@web_app.route("/logout")
def logout():
    flask.session.pop("admin", None)
    return flask.redirect("/login")


@web_app.before_request
def require_login():
    """未登录拦截：页面跳登录，API 返回 401；静态资源放行。"""
    if not _auth_enabled() or _is_authed():
        return
    endpoint = flask.request.endpoint or ""
    if endpoint in ("stock_web.login", "stock_web.serve_report", "stock_web.serve_chart"):
        return
    if flask.request.path.startswith("/api/"):
        return flask.jsonify({"status": "error", "error": "未登录"}), 401
    return flask.redirect(flask.url_for("stock_web.login", next=flask.request.path))


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
    app.secret_key = os.getenv("SECRET_KEY", "stock-invest-tool-dev-secret")
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
