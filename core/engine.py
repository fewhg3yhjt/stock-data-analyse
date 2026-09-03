"""统一分析引擎 — 抽取 main.py / web/app.py 重复的分析管线

CLI 和 Web 共用此引擎，保证两条入口的行为一致。

管线: 数据获取 → 技术指标 → 估值分析 → 策略买入计划 → 回测 → LLM Prompt → 报告
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from StockInvestmentTool.config import Config
from StockInvestmentTool.core.registry import SchemeRegistry
from StockInvestmentTool.core.scheme import SchemeConfig
from StockInvestmentTool.datasource.fetcher import StockDataFetcher
from StockInvestmentTool.datasource.base import DataSource, FallbackDataSource
from StockInvestmentTool.datasource.indicators import TechnicalIndicators, ValuationHelper
from StockInvestmentTool.strategy.multi_buy import MultiBuyStrategy
from StockInvestmentTool.backtest.engine import BacktestEngine
from StockInvestmentTool.backtest.metrics import PerformanceMetrics
from StockInvestmentTool.analysis.report import ReportGenerator
from StockInvestmentTool.analysis.charts import ChartGenerator
from StockInvestmentTool.prompt.builder import PromptBuilder
from StockInvestmentTool.prompt.llm_client import DeepSeekClient, LLMError

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 数据类
# ═══════════════════════════════════════════════════════════════

@dataclass
class AnalysisOptions:
    """单次分析选项"""
    do_backtest: bool = True
    do_prompt: bool = False
    do_api: bool = False
    skip_charts: bool = False
    stock_type: str = "B"
    initial_cash: Optional[float] = None   # None → 用方案配置

    # 回测模式: 默认参数网格搜索优化；为 True 时跳过优化用固定参数
    no_optimize: bool = False
    trail_threshold: Optional[float] = None
    auto_optimize: bool = False

    # 可选: LLM 流式输出
    api_stream: bool = False


@dataclass
class AnalysisResult:
    """一次分析的结构化结果"""
    code: str
    name: str
    scheme_name: str
    asset_type: str
    kline: Optional[pd.DataFrame] = None
    technical: dict = field(default_factory=dict)
    valuation: dict = field(default_factory=dict)
    cross_support: dict = field(default_factory=dict)
    buy_plan: list = field(default_factory=list)
    backtest_result: Optional[dict] = None
    backtest_metrics: Optional[dict] = None
    llm_analysis: Optional[str] = None
    prompt_path: Optional[str] = None
    chart_kline_path: Optional[str] = None
    chart_backtest_path: Optional[str] = None
    chart_perf_path: Optional[str] = None
    report_path: Optional[str] = None
    data_sources: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转前端可用 dict"""
        d = {
            "status": "success",
            "asset_type": self.asset_type,
            "stock_name": self.name,
            "stock_code": self.code,
            "scheme_name": self.scheme_name,
            "current_price": self.technical.get("current_price"),
            "trend": self.technical.get("trend"),
            "volatility": self.technical.get("volatility"),
            "data_sources": self.data_sources,
        }
        if self.kline is not None and len(self.kline):
            d["data_start"] = str(self.kline["date"].iloc[0])[:10]
            d["data_end"] = str(self.kline["date"].iloc[-1])[:10]
            if "high" in self.kline.columns:
                d["year_high"] = float(self.kline["high"].tail(252).max())
                d["year_high_as_of"] = d["data_end"]
        pe_info = self.valuation.get("pe") or {}
        d["pe"] = pe_info.get("current_pe")
        d["pe_percentile"] = pe_info.get("pe_percentile")
        d["buy_plan"] = self.buy_plan
        d["support_levels"] = self.cross_support.get("all_levels", {})
        d["strong_source"] = self.cross_support.get("strong_source", "")
        d["weak_source"] = self.cross_support.get("weak_source", "")
        if self.report_path:
            d["report_file"] = Path(self.report_path).name
        d["charts"] = []
        for name, path in [("K线图", self.chart_kline_path),
                           ("回测图", self.chart_backtest_path),
                           ("绩效图", self.chart_perf_path)]:
            if path:
                d["charts"].append({"name": name, "file": Path(path).name})
        if self.backtest_result and self.backtest_metrics:
            d["backtest_done"] = True
            best = self.backtest_result.get("best_params",
                                            self.backtest_result.get("custom_params", {}))
            _trail_val = best.get("trail_threshold")
            d["best_trail"] = f"{float(_trail_val) * 100:.0f}%" if _trail_val is not None else "按方案配置"
            d["best_offset"] = f"{best.get('offset', 0) * 100:+.0f}%"
            for k in ["total_return", "buy_hold_return", "max_drawdown",
                      "sharpe_ratio", "win_rate", "trade_count", "final_asset"]:
                d[k] = self.backtest_metrics.get(k)
            d["trades"] = self.backtest_result.get("backtest", {}).get("trades", [])
            d["stop_loss_rate"] = Config.STOP_LOSS_RATE
            d["drawdown_stop"] = Config.DRAWDOWN_STOP
            d["min_profit_for_dd"] = Config.MIN_PROFIT_FOR_DD
        if self.prompt_path:
            d["prompt_file"] = Path(self.prompt_path).name
            try:
                d["prompt_content"] = Path(self.prompt_path).read_text(encoding="utf-8")
            except OSError:
                pass
        if self.llm_analysis:
            d["llm_result"] = self.llm_analysis[:2000] if len(self.llm_analysis) > 2000 else self.llm_analysis
        return d


# ═══════════════════════════════════════════════════════════════
# 分析引擎
# ═══════════════════════════════════════════════════════════════

class AnalysisEngine:
    """统一分析引擎

    Args:
        scheme_name: 策略方案名，默认 default_value
        registry: 可选注入的方案注册中心（默认新建）
    """

    def __init__(self, scheme_name: str = "default_value",
                 registry: Optional[SchemeRegistry] = None,
                 data_source: Optional[DataSource] = None,
                 scheme: Optional[SchemeConfig] = None):
        self.registry = registry or SchemeRegistry()
        self.scheme: SchemeConfig = scheme or self.registry.get(scheme_name)
        self.data_source = data_source or FallbackDataSource()

    # ── 数据获取 ─────────────────────────────────────────

    def _fetch(self, fetcher: StockDataFetcher, code: str,
               start_date: str, end_date: str) -> dict:
        """获取 K线 + 基本面 + 分红"""
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        profit_year = end_dt.year
        profit_q = ((end_dt.month - 1) // 3) or 4
        if profit_q == 4:
            profit_year -= 1

        kline = None
        try:
            kline = self.data_source.fetch_kline(code, start_date, end_date)
        except Exception as e:
            logger.warning("统一数据源 K线读取失败(%s)，回退 baostock: %s", code, e)
        if kline is None or kline.empty:
            kline = fetcher.get_kline(code=code, start_date=start_date, end_date=end_date)
        from StockInvestmentTool.datasource.base import WarehouseSource
        warehouse_source = WarehouseSource()
        basic = warehouse_source.fetch_instrument(code)
        basic_source = "warehouse" if basic else "online_fallback"
        fundamentals = warehouse_source.fetch_fundamental_history(code)
        profit = {}
        if not fundamentals.empty:
            latest = fundamentals.sort_values("stat_date").iloc[-1]
            profit = latest.to_dict()
            profit_source = "warehouse"
        else:
            profit = fetcher.get_profit_data(code, profit_year, profit_q)
            profit_source = "online_fallback"
        divs = []
        for y in range(profit_year - 5, profit_year + 1):
            divs.extend(fetcher.get_dividend_data(code, y))

        return {"kline": kline, "basic": basic, "profit": profit, "dividends": divs,
                "data_sources": {"basic": basic_source, "fundamentals": profit_source,
                                  "dividends": "online_fallback"}}

    # ── 技术指标 ─────────────────────────────────────────

    @staticmethod
    def _technical(kline: pd.DataFrame) -> tuple[dict, dict]:
        """计算技术指标 + 支撑/压力位"""
        sr = TechnicalIndicators.support_resistance(kline)
        trend = TechnicalIndicators.trend_judgment(kline)
        vol = TechnicalIndicators.annualized_volatility(kline)
        last = kline.iloc[-1]

        technical = {
            **sr,
            "trend": trend,
            "volatility": vol,
        }
        for col in ("ma20", "ma60", "ma120"):
            technical[col] = round(float(last.get(col, 0)), 2) if col in kline.columns else None
        return technical, sr

    # ── 估值分析 ─────────────────────────────────────────

    @staticmethod
    def _valuation(kline: pd.DataFrame, technical: dict,
                   dividends: list) -> tuple[dict, dict, Optional[float]]:
        """估值分析，返回 (valuation, cross_support, dividend_anchor)"""
        pe_info = ValuationHelper.pe_percentile(kline)
        anchor = ValuationHelper.triple_anchor(dividends, technical["current_price"])
        if "ma60" not in technical:
            technical["ma60"] = 0
        cross = ValuationHelper.cross_validation(technical, anchor)
        valuation = {"pe": pe_info, "triple_anchor": anchor}

        dividend_anchor = None
        if anchor and anchor.get("anchor_price_3"):
            dividend_anchor = anchor["anchor_price_3"]
        return valuation, cross, dividend_anchor

    # ── 买入计划 ─────────────────────────────────────────

    def _buy_plan(self, kline: pd.DataFrame, current_price: float,
                  dividend_anchor: Optional[float]) -> list[dict]:
        """用方案配置生成买入计划"""
        from StockInvestmentTool.indicators.context import IndicatorContext
        from StockInvestmentTool.strategy.context import RuleContext
        from StockInvestmentTool.strategy.rule_registry import dispatch_rule

        rule = self.scheme.rule("buy", "support_level")
        if rule is not None:
            ctx = RuleContext(
                row=kline.iloc[-1], df=kline,
                indicators=IndicatorContext(kline),
                current_price=current_price,
                dividend_anchor=dividend_anchor,
                extra={"scheme": self.scheme},
            )
            result = dispatch_rule("buy", rule.type, ctx, rule.params)
            plan = result.detail.get("plan") if result and result.detail else None
            if plan is not None:
                return plan
        strategy = MultiBuyStrategy(
            dividend_anchor=dividend_anchor,
            scheme=self.scheme,
        )
        return strategy.generate_plan(kline, current_price)

    # ── 回测 ─────────────────────────────────────────────

    def _backtest(self, kline: pd.DataFrame, name: str, code: str,
                  dividend_anchor: Optional[float],
                  options: AnalysisOptions) -> tuple[Optional[dict], Optional[dict], Optional[str], Optional[str]]:
        """回测 + 图表，返回 (result, metrics, chart_backtest, chart_perf)"""
        if not options.do_backtest:
            return None, None, None, None

        engine = BacktestEngine(
            df=kline,
            initial_cash=options.initial_cash or self.scheme.backtest.initial_cash,
            stock_type=options.stock_type,
            dividend_anchor=dividend_anchor,
            scheme=self.scheme,
        )
        if options.no_optimize:
            bt = engine.run_custom(
                trail_threshold=options.trail_threshold,
                offset=0.0,
            )
        else:
            bt = engine.optimize_and_backtest()
        metrics = PerformanceMetrics.summary(bt)
        detail = bt.get("backtest", {})
        params = bt.get("best_params", bt.get("custom_params", {}))

        chart_backtest = chart_perf = None
        if not options.skip_charts:
            try:
                charts = ChartGenerator()
                safe_code = code.replace(".", "_")
                chart_backtest = charts.plot_backtest(
                    df=kline, trades=detail.get("trades", []),
                    equity_curve=detail.get("equity_curve", []),
                    stock_name=name, stock_code=safe_code,
                    trail_threshold=params.get("trail_threshold", 0.05),
                )
                chart_perf = charts.plot_performance_summary(
                    equity_curve=detail.get("equity_curve", []), stock_code=safe_code,
                )
            except Exception as e:
                logger.error("回测图表生成失败: %s", e)

        return bt, metrics, chart_backtest, chart_perf

    # ── LLM ─────────────────────────────────────────────

    def _llm(self, name: str, code: str, technical: dict, valuation: dict,
             cross_support: dict, buy_plan: list, pe_info: dict,
             asset_type: str, options: AnalysisOptions) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """生成 Prompt + 可选调用 LLM，返回 (prompt_path, llm_result, prompt_text)"""
        if not (options.do_prompt or options.do_api):
            return None, None, None

        builder = PromptBuilder()
        if asset_type == "etf":
            prompt = builder.build_fund_prompt(
                fund_name=name, fund_code=code,
                technical=technical, cross_support=cross_support,
            )
        else:
            prompt = builder.build_stock_prompt(
                stock_name=name, stock_code=code,
                technical=technical, valuation=valuation,
                cross_support=cross_support, strategy_plan=buy_plan, pe_info=pe_info,
            )
        prompt_path = builder.save_prompt_file(prompt, code)

        llm_result = None
        if options.do_api:
            try:
                client = DeepSeekClient()
                if options.api_stream:
                    llm_result = client.analyze_stream(prompt)
                else:
                    llm_result = client.analyze(prompt)
            except (LLMError, Exception) as e:
                logger.warning("LLM 分析失败: %s", e)
                llm_result = f"⚠️ LLM 分析失败: {e}"

        return prompt_path, llm_result, prompt

    # ── 报告 ─────────────────────────────────────────────

    def _report(self, result: AnalysisResult,
                chart_kline_path: Optional[str]) -> Optional[str]:
        """生成 Markdown 报告"""
        try:
            report = ReportGenerator()
            return report.generate(
                stock_name=result.name,
                stock_code=result.code,
                kline_df=result.kline,
                technical=result.technical,
                valuation=result.valuation,
                cross_support=result.cross_support,
                strategy_plan=result.buy_plan,
                backtest_result=result.backtest_result,
                backtest_metrics=result.backtest_metrics,
                chart_kline_path=chart_kline_path,
                chart_backtest_path=result.chart_backtest_path,
                llm_analysis=result.llm_analysis,
                llm_prompt_path=result.prompt_path,
            )
        except Exception as e:
            logger.error("报告生成失败: %s", e)
            return None

    # ── 主入口 ───────────────────────────────────────────

    def analyze(self, code: str, name: str,
                start_date: Optional[str] = None,
                end_date: Optional[str] = None,
                options: Optional[AnalysisOptions] = None,
                progress_callback=None) -> AnalysisResult:
        """一键分析，返回结构化结果

        Args:
            code: 股票代码 (sh.600900 / 600900)
            name: 股票名称
            start_date / end_date: YYYY-MM-DD，默认近1年
            options: 分析选项
            progress_callback: 可选回调 fn(stage: str, progress: int)，用于 UI 进度显示
        """
        options = options or AnalysisOptions()
        code = StockDataFetcher.normalize_code(code)

        if end_date is None:
            end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        result = AnalysisResult(
            code=code,
            name=name,
            scheme_name=self.scheme.name,
            asset_type=StockDataFetcher.detect_type(code),
        )

        def _progress(stage: str, progress: int):
            if progress_callback:
                try:
                    progress_callback(stage, progress)
                except Exception:
                    pass

        # 1. 数据获取
        _progress("获取数据", 10)
        with StockDataFetcher() as fetcher:
            data = self._fetch(fetcher, code, start_date, end_date)
        result.kline = data["kline"]
        result.data_sources = data.get("data_sources", {})

        # 2. 技术指标
        _progress("技术分析", 30)
        result.kline = TechnicalIndicators.compute_all(result.kline)
        result.technical, sr = self._technical(result.kline)

        # 3. 估值分析
        _progress("估值分析", 45)
        result.valuation, result.cross_support, dividend_anchor = self._valuation(
            result.kline, result.technical, data["dividends"]
        )

        # 4. 买入计划
        _progress("策略生成", 60)
        result.buy_plan = self._buy_plan(
            result.kline, sr["current_price"], dividend_anchor
        )

        # 5. 回测
        _progress("策略回测", 70)
        result.backtest_result, result.backtest_metrics, \
            result.chart_backtest_path, result.chart_perf_path = self._backtest(
                result.kline, name, code, dividend_anchor, options
            )

        # 6. LLM
        _progress("生成Prompt", 85)
        pe_info = (result.valuation.get("pe") or {})
        result.prompt_path, result.llm_analysis, _ = self._llm(
            name, code, result.technical, result.valuation, result.cross_support,
            result.buy_plan, pe_info, result.asset_type, options
        )

        # 7. 报告
        _progress("生成报告", 95)
        chart_kline = None
        if not options.skip_charts:
            try:
                chart_kline = ChartGenerator().plot_kline(
                    result.kline, name, stock_code=code.replace(".", "_")
                )
            except Exception as e:
                logger.error("K线图生成失败: %s", e)
        result.chart_kline_path = chart_kline
        result.report_path = self._report(result, chart_kline)

        return result
