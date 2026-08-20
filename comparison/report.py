"""对比报告生成 — Markdown 对比表 + 对比图表

- build_markdown(report)     → 对比表格（Markdown）
- build_charts(report, name) → 收益率柱状图 + 权益曲线叠加图（PNG）
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

from StockInvestmentTool.config import Config
from StockInvestmentTool.comparison.runner import ComparisonReport
from StockInvestmentTool.analysis.charts import setup_cjk_font

logger = logging.getLogger(__name__)

# 复用 charts.py 的中文字体配置（幂等）
setup_cjk_font()

# 对比指标列表（显示顺序）
_METRIC_ROWS = [
    ("total_return", "总收益率", "{:.2f}%"),
    ("buy_hold_return", "买入持有", "{:.2f}%"),
    ("excess_return", "超额收益", "{:+.2f}%"),
    ("max_drawdown", "最大回撤", "{:.2f}%"),
    ("sharpe_ratio", "夏普比率", "{:.2f}"),
    ("calmar_ratio", "Calmar", "{:.2f}"),
    ("win_rate", "胜率", "{:.1f}%"),
    ("trade_count", "交易次数", "{}"),
    ("final_asset", "最终资产", "{:,.0f}"),
]


def _metric_str(metrics: dict, key: str, fmt: str) -> str:
    """取指标值并格式化，缺省显示 '—'"""
    val = metrics.get(key)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "—"
    if key == "max_drawdown":
        return fmt.format(val)
    return fmt.format(val)


def build_markdown(report: ComparisonReport) -> str:
    """生成 Markdown 对比表"""
    lines: list[str] = []
    lines.append(f"# 多方案对比报告：{report.name} ({report.code})")
    lines.append("")
    lines.append(f"> 对比区间：{report.start_date} ~ {report.end_date} · 股票类型：{report.stock_type}")
    lines.append("")

    results = report.ranking()

    # 排名摘要
    lines.append("## 收益率排名")
    lines.append("")
    lines.append("| 排名 | 方案 | 总收益率 | 相对最佳 |")
    lines.append("|---|---|---|---|")
    best = results[0].total_return if results else 0
    for i, r in enumerate(results, 1):
        diff = r.total_return - best
        marker = "🏆" if i == 1 else ""
        lines.append(f"| {i} | **{r.scheme_name}** {marker} | **{r.total_return:.2f}%** | {diff:+.2f}% |")
    lines.append("")

    # 指标明细表（方案为列）
    lines.append("## 指标明细")
    lines.append("")
    lines.append("| 指标 | " + " | ".join(r.scheme_name for r in results) + " |")
    lines.append("|---|" + "---|" * len(results))
    for key, label, fmt in _METRIC_ROWS:
        row = [f"**{label}**"]
        for r in results:
            row.append(_metric_str(r.metrics, key, fmt))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # 最优参数
    lines.append("## 最优参数")
    lines.append("")
    lines.append("| 方案 | 右侧回撤 | 买入偏移 |")
    lines.append("|---|---|---|")
    for r in results:
        trail = r.params.get("trail_threshold")
        offset = r.params.get("offset")
        trail_s = f"{trail*100:.0f}%" if isinstance(trail, (int, float)) else "—"
        offset_s = f"{offset*100:+.0f}%" if isinstance(offset, (int, float)) else "—"
        lines.append(f"| {r.scheme_name} | {trail_s} | {offset_s} |")
    lines.append("")

    # 图表引用
    for chart_name in ("对比柱状图", "权益曲线对比"):
        lines.append(f"![{chart_name}]({chart_name}.png)")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*本报告由 StockInvestmentTool 自动生成，仅供参考，不构成投资建议。*")
    return "\n".join(lines)


def build_charts(report: ComparisonReport, output_dir: Optional[Path] = None) -> dict[str, str]:
    """生成对比图表

    Returns
    -------
    dict: {chart_name: file_path}
    """
    out_dir = Path(output_dir) if output_dir else Config.CHART_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_code = report.code.replace(".", "_").replace("/", "_")
    results = [r for r in report.results if "total_return" in r.metrics]

    chart_paths: dict[str, str] = {}

    if not results:
        logger.warning("无有效回测结果，跳过图表生成")
        return chart_paths

    # ── 柱状图：各方案总收益率 ──
    names = [r.scheme_name for r in results]
    returns = [r.total_return for r in results]
    colors = ["#1a73e8" if r.total_return == max(returns) else "#a0aec0" for r in results]

    fig, ax = plt.subplots(figsize=(Config.CHART_FIGSIZE[0] * 0.8, Config.CHART_FIGSIZE[1] * 0.6))
    bars = ax.bar(names, returns, color=colors, width=0.55)
    ax.axhline(0, color="#cbd5e1", linewidth=0.8)
    for bar, val in zip(bars, returns):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + (0.3 if val >= 0 else -1.2),
                f"{val:.2f}%", ha="center", va="bottom" if val >= 0 else "top", fontsize=11, fontweight="bold")
    ax.set_title(f"{report.name} 多方案收益率对比", fontsize=13)
    ax.set_ylabel("总收益率 (%)")
    ax.set_ylim(min(returns) - 4, max(returns) + 4)
    ax.grid(axis="y", alpha=0.3)
    for s in ["top", "right"]:
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    bar_path = str(out_dir / f"{safe_code}_对比柱状图.png")
    fig.savefig(bar_path, dpi=Config.CHART_DPI)
    plt.close(fig)
    chart_paths["对比柱状图"] = bar_path
    logger.info("对比柱状图已保存: %s", bar_path)

    # ── 权益曲线叠加图 ──
    fig2, ax2 = plt.subplots(figsize=(Config.CHART_FIGSIZE))
    palette = ["#1a73e8", "#e67e22", "#28a745", "#dc3545", "#9b59b6", "#16a085"]
    for i, r in enumerate(results):
        if not r.equity_curve:
            continue
        df = pd.DataFrame(r.equity_curve)
        if df.empty or "date" not in df.columns:
            continue
        # 归一化到初始资金，便于对比
        init = df["total_asset"].iloc[0] if len(df) else 1
        norm = df["total_asset"] / init * 100 - 100  # 相对收益率 %
        ax2.plot(df["date"], norm, label=r.scheme_name, linewidth=1.6,
                 color=palette[i % len(palette)])
    ax2.axhline(0, color="#cbd5e1", linewidth=0.8)
    ax2.set_title(f"{report.name} 各方案权益曲线对比（相对收益率 %）", fontsize=13)
    ax2.set_ylabel("相对收益率 (%)")
    ax2.set_xlabel("日期")
    ax2.legend(loc="best", fontsize=10)
    ax2.grid(alpha=0.3)
    for s in ["top", "right"]:
        ax2.spines[s].set_visible(False)
    fig2.tight_layout()
    curve_path = str(out_dir / f"{safe_code}_权益曲线对比.png")
    fig2.savefig(curve_path, dpi=Config.CHART_DPI)
    plt.close(fig2)
    chart_paths["权益曲线对比"] = curve_path
    logger.info("权益曲线对比图已保存: %s", curve_path)

    return chart_paths


def write_report(report: ComparisonReport, output_dir: Optional[Path] = None) -> tuple[str, dict]:
    """生成对比报告文件 + 图表

    Returns
    -------
    (report_path, chart_paths)
    """
    out_dir = Path(output_dir) if output_dir else Config.REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    md = build_markdown(report)
    safe_code = report.code.replace(".", "_").replace("/", "_")
    fname = f"{safe_code}_对比报告_{pd.Timestamp.now():%Y%m%d_%H%M}.md"
    path = out_dir / fname
    path.write_text(md, encoding="utf-8")

    charts = build_charts(report)
    return str(path), charts
