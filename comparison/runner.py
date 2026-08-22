"""多方案对比执行器 — 对同一股票运行多个策略方案并横向对比

设计要点:
  - 数据获取、指标计算、估值分析只做一次（多方案共享），仅回测按方案分别执行
  - 保证对比公平: 所有方案使用相同的 K 线、相同的股息率锚
  - 返回结构化 ComparisonReport，供 CLI 表格 / Web 图表 / Markdown 报告共用
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from StockInvestmentTool.core.registry import SchemeRegistry
from StockInvestmentTool.core.scheme import SchemeConfig
from StockInvestmentTool.datasource.fetcher import StockDataFetcher
from StockInvestmentTool.datasource.indicators import TechnicalIndicators
from StockInvestmentTool.backtest.engine import BacktestEngine
from StockInvestmentTool.backtest.metrics import PerformanceMetrics

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 数据类
# ═══════════════════════════════════════════════════════════════

@dataclass
class SchemeComparisonResult:
    """单个方案的回测结果"""
    scheme_name: str
    scheme_description: str
    metrics: dict                          # PerformanceMetrics.summary 输出
    params: dict                           # 最优/自定义参数
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)

    @property
    def total_return(self) -> float:
        return float(self.metrics.get("total_return", 0))


@dataclass
class ComparisonReport:
    """多方案对比结果"""
    code: str
    name: str
    start_date: str
    end_date: str
    stock_type: str
    results: list[SchemeComparisonResult] = field(default_factory=list)

    def ranking(self) -> list[SchemeComparisonResult]:
        """按总收益率降序排列"""
        return sorted(self.results, key=lambda r: r.total_return, reverse=True)

    def to_dict(self) -> dict:
        """转 JSON 安全的 dict（供 Web 展示）"""
        return {
            "code": self.code,
            "name": self.name,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "stock_type": self.stock_type,
            "results": [
                {
                    "scheme_name": r.scheme_name,
                    "scheme_description": r.scheme_description,
                    "metrics": r.metrics,
                    "params": r.params,
                    "trades": r.trades,
                    "equity_curve": r.equity_curve,
                }
                for r in self.results
            ],
        }


# ═══════════════════════════════════════════════════════════════
# 对比执行器
# ═══════════════════════════════════════════════════════════════

class MultiSchemeRunner:
    """多方案对比执行器

    Args:
        scheme_names: 参与对比的方案名称列表
        registry: 可选注入的方案注册中心
    """

    def __init__(self, scheme_names: list[str],
                 registry: Optional[SchemeRegistry] = None):
        self.scheme_names = list(scheme_names)
        self.registry = registry or SchemeRegistry()
        self.schemes: list[SchemeConfig] = [
            self.registry.get(n) for n in self.scheme_names
        ]

    # ── 共享数据准备 ───────────────────────────────────

    def _prepare_data(self, code: str, start_date: str, end_date: str) -> tuple:
        """获取并计算共享数据（K线 + 技术指标 + 股息率锚）

        Returns
        -------
        (kline, dividend_anchor)
        """
        with StockDataFetcher() as fetcher:
            kline = fetcher.get_kline(code=code, start_date=start_date, end_date=end_date)

            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            py = end_dt.year
            pq = ((end_dt.month - 1) // 3) or 4
            if pq == 4:
                py -= 1
            divs = []
            for y in range(py - 5, py + 1):
                divs.extend(fetcher.get_dividend_data(code, y))

        kline = TechnicalIndicators.compute_all(kline)
        sr = TechnicalIndicators.support_resistance(kline)

        # 股息率极端低估锚（anchor_price_3）
        dividend_anchor = None
        try:
            from StockInvestmentTool.datasource.indicators import ValuationHelper
            anchor = ValuationHelper.triple_anchor(divs, sr["current_price"])
            if anchor and anchor.get("anchor_price_3"):
                dividend_anchor = anchor["anchor_price_3"]
        except Exception as e:
            logger.warning("股息率锚计算失败: %s", e)

        return kline, dividend_anchor

    def _run_one(self, scheme: SchemeConfig, kline, dividend_anchor,
                 stock_type: str, initial_cash: Optional[float],
                 no_optimize: bool = False,
                 trail_threshold: float = 0.05) -> SchemeComparisonResult:
        """运行单个方案的回测"""
        engine = BacktestEngine(
            df=kline,
            initial_cash=initial_cash or scheme.backtest.initial_cash,
            stock_type=stock_type,
            dividend_anchor=dividend_anchor,
            scheme=scheme,
        )
        if no_optimize:
            bt = engine.run_custom(trail_threshold=trail_threshold, offset=0.0)
        else:
            bt = engine.optimize_and_backtest()

        metrics = PerformanceMetrics.summary(bt)
        detail = bt.get("backtest", {})
        params = bt.get("best_params", bt.get("custom_params", {}))

        return SchemeComparisonResult(
            scheme_name=scheme.name,
            scheme_description=scheme.description,
            metrics=metrics,
            params=params,
            trades=detail.get("trades", []),
            equity_curve=detail.get("equity_curve", []),
        )

    # ── 主入口 ─────────────────────────────────────────

    def compare(self, code: str, name: str,
                start_date: Optional[str] = None,
                end_date: Optional[str] = None,
                stock_type: str = "B",
                initial_cash: Optional[float] = None,
                no_optimize: bool = False,
                trail_threshold: float = 0.05,
                progress_callback=None) -> ComparisonReport:
        """对同一股票运行所有方案并对比

        Args:
            code: 股票代码
            name: 股票名称
            start_date / end_date: 分析区间
            stock_type: 股票类型 (A/B/C/D)
            initial_cash: 初始资金，None → 用各自方案配置
            no_optimize: 跳过参数优化
            progress_callback: fn(stage, progress)
        """
        code = StockDataFetcher.normalize_code(code)
        if end_date is None:
            end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        report = ComparisonReport(
            code=code, name=name,
            start_date=start_date, end_date=end_date,
            stock_type=stock_type,
        )

        # 1. 共享数据（只取一次）
        if progress_callback:
            progress_callback("获取数据", 10)
        kline, dividend_anchor = self._prepare_data(code, start_date, end_date)

        # 2. 逐方案回测
        total = len(self.schemes)
        for i, scheme in enumerate(self.schemes):
            if progress_callback:
                progress_callback(f"回测方案 {scheme.name} ({i+1}/{total})", 20 + int(70 * (i + 1) / total))
            try:
                result = self._run_one(
                    scheme, kline, dividend_anchor, stock_type,
                    initial_cash, no_optimize, trail_threshold,
                )
                report.results.append(result)
                logger.info("方案 %s 回测完成: 收益 %.2f%%", scheme.name, result.total_return)
            except Exception as e:
                logger.error("方案 %s 回测失败: %s", scheme.name, e)
                # 失败的方案加入空结果，保持对比可见性
                report.results.append(SchemeComparisonResult(
                    scheme_name=scheme.name,
                    scheme_description=scheme.description,
                    metrics={"error": str(e)},
                    params={},
                ))

        if progress_callback:
            progress_callback("完成", 100)
        return report
