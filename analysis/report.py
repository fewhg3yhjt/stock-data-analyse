"""Markdown 报告生成 — 整合基本面 + 技术面 + 回测 + LLM 分析"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from StockInvestmentTool.config import Config

logger = logging.getLogger(__name__)


class ReportGenerator:
    """生成 Markdown 格式的综合分析报告"""

    def __init__(self, output_dir: Optional[Path] = None):
        self.output_dir = Path(output_dir) if output_dir else Config.REPORT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(
        self,
        stock_name: str,
        stock_code: str,
        kline_df: "DataFrame",   # noqa: F821
        technical: dict,
        valuation: dict,
        cross_support: dict,
        strategy_plan: list[dict],
        backtest_result: Optional[dict] = None,
        backtest_metrics: Optional[dict] = None,
        chart_kline_path: Optional[str] = None,
        chart_backtest_path: Optional[str] = None,
        llm_analysis: Optional[str] = None,
        llm_prompt_path: Optional[str] = None,
    ) -> str:
        """生成完整 Markdown 报告

        Returns
        -------
        str
            报告文件路径
        """
        lines: list[str] = []

        # ── 标题 ──
        lines.append(f"# 股票分析报告：{stock_name} ({stock_code})")
        lines.append("")
        lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
        lines.append("")
        lines.append("---")
        lines.append("")

        # ── 一、基本信息 ──
        lines.append("## 一、基本信息")
        lines.append("")
        lines.append("| 项目 | 内容 |")
        lines.append("|---|---|")
        lines.append(f"| 股票名称 | {stock_name} |")
        lines.append(f"| 股票代码 | {stock_code} |")
        lines.append(f"| 分析区间 | {kline_df['date'].min().strftime('%Y-%m-%d')} ~ {kline_df['date'].max().strftime('%Y-%m-%d')} |")
        lines.append(f"| 总交易日 | {len(kline_df)} 天 |")
        lines.append(f"| 数据源 | baostock |")
        lines.append("")

        # ── 二、价格统计 ──
        lines.append("## 二、价格统计")
        lines.append("")
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|---|---|")
        lines.append(f"| 当前价 | {technical.get('current_price', 'N/A')} 元 |")
        lines.append(f"| 区间最高 | {technical.get('year_high', 'N/A')} 元 |")
        lines.append(f"| 区间最低 | {technical.get('year_low', 'N/A')} 元 |")
        lines.append(f"| 近3月低点 | {technical.get('recent_low', 'N/A')} 元 |")
        lines.append(f"| 年化波动率 | {technical.get('volatility', 'N/A')}% |")
        lines.append("")

        # ── 三、技术分析 ──
        lines.append("## 三、技术分析")
        lines.append("")

        # 均线状态
        lines.append("### 均线状态")
        lines.append("")
        lines.append("| 均线 | 数值(元) |")
        lines.append("|---|---|")
        for col, label in [("ma5", "MA5"), ("ma20", "MA20"),
                           ("ma60", "MA60"), ("ma120", "MA120")]:
            val = kline_df[col].iloc[-1] if col in kline_df.columns else None
            if val is not None:
                rel = "⬆ 上方" if kline_df["close"].iloc[-1] > val else "⬇ 下方"
                lines.append(f"| {label} | {val:.2f} ({rel}) |")
        lines.append("")
        lines.append(f"**趋势判定：** {technical.get('trend', 'N/A')}")
        lines.append("")

        # 支撑/压力位
        lines.append("### 支撑位与压力位")
        lines.append("")
        lines.append("| 类型 | 价位(元) | 来源 |")
        lines.append("|---|---|---|")

        if cross_support:
            for level_name, price, src in [
                ("综合强支撑", cross_support.get("strong_support"), cross_support.get("strong_source")),
                ("综合弱支撑", cross_support.get("weak_support"), cross_support.get("weak_source")),
            ]:
                if price:
                    lines.append(f"| **{level_name}** | **{price}** | {src} |")

            if "all_levels" in cross_support:
                for src, price in cross_support["all_levels"].items():
                    lines.append(f"| {src} | {price} | — |")

        lines.append(f"| 近12月高点 | {technical.get('year_high', 'N/A')} | 止盈上限参考 |")
        lines.append(f"| 近12月低点 | {technical.get('year_low', 'N/A')} | 极限支撑参考 |")
        lines.append("")

        # 支撑位取法说明（操作逻辑）
        lines.append("### 支撑位取法说明")
        lines.append("")
        lines.append("- **综合强支撑** = 候选值取**最低值**（MA60 / 近3月低点 / 年内低点）")
        lines.append("- **综合弱支撑** = 候选值取**次低值**（MA60 / 近3月低点 / 年内低点）")
        lines.append("- **极端低估锚** = 每股分红 ÷ 4.0%（股息率锚，不参与强/弱支撑竞争）")
        lines.append("")

        # 买入触发价计算（含回测最优偏移）
        lines.append("### 买入触发价计算")
        lines.append("")
        bt_offset = 0.0
        if backtest_result:
            params = backtest_result.get("best_params", backtest_result.get("custom_params", {}))
            try:
                bt_offset = float(params.get("offset", 0.0))
            except (TypeError, ValueError):
                bt_offset = 0.0
        lines.append(f"> 买入偏移量由回测网格搜索选择：触发价 = 支撑位 × (1 + 偏移)，"
                     f"本回测最优偏移 **{bt_offset:+.0%}**。")
        lines.append("")
        for p in strategy_plan:
            comp = p.get("computation")
            if comp and comp.get("base_value"):
                label = p.get("label", f"第{p['stage']}批")
                base = comp["base_value"]
                trig = comp.get("trigger") or comp.get("threshold")
                method = comp.get("method", "")
                lines.append(f"- **第{p['stage']}批（{label}）**：{method} = **{base}**，"
                             f"触发价 = {base} × (1 + 偏移) = **{trig}** 元")
            else:
                lines.append(f"- **第{p['stage']}批（{p.get('label', '?')}）**：触发价 {p.get('threshold')} 元")
        lines.append("")

        # ── 四、估值分析 ──
        lines.append("## 四、估值分析")
        lines.append("")

        pe_info = valuation.get("pe", {})
        if pe_info:
            lines.append("### PE 估值")
            lines.append("")
            lines.append("| 指标 | 数值 |")
            lines.append("|---|---|")
            lines.append(f"| PE(TTM) | {pe_info.get('current_pe', 'N/A')} |")
            lines.append(f"| PE 历史分位 | {pe_info.get('pe_percentile', 'N/A')}% |")
            lines.append(f"| PE 中位数 | {pe_info.get('pe_median', 'N/A')} |")
            lines.append("")

        anchor = valuation.get("triple_anchor", {})
        if anchor and anchor.get("div_per_share"):
            lines.append("### 股息率三重锚定价")
            lines.append("")
            lines.append("| 锚类型 | 价格(元) | 说明 |")
            lines.append("|---|---|---|")
            lines.append(f"| 锚定价①（历史均值） | {anchor.get('anchor_price_1', 'N/A')} | 每股分红 {anchor.get('div_per_share', 'N/A')}元 ÷ 近5年平均股息率 {anchor.get('avg_5y_yield', 'N/A')}% |")
            lines.append(f"| 锚定价②（安全边际 3.4%） | {anchor.get('anchor_price_2', 'N/A')} | 适合稳健建仓 |")
            lines.append(f"| 锚定价③（极端低估 4.0%） | {anchor.get('anchor_price_3', 'N/A')} | 极端机会建仓 |")
            lines.append("")
            lines.append(f"**判定：** {anchor.get('judgment', 'N/A')}")
            lines.append("")

        # ── 五、买入策略 ──
        lines.append("## 五、分批买入策略")
        lines.append("")
        lines.append("| 批次 | 触发条件 | 仓位比例 | 触发状态 |")
        lines.append("|---|---|---|---|")
        for p in strategy_plan:
            status = "✅ 已触发" if p["triggered"] else "⏳ 未触发"
            label = p.get('label', p.get('ma_col', '?'))
            lines.append(f"| 第{p['stage']}批 | ≤ {p['threshold']}元 ({label}) | {int(p['ratio'] * 100)}% | {status} |")
        lines.append("")

        # ── 六、策略回测（如有） ──
        if backtest_result and backtest_metrics:
            lines.append("## 六、策略回测")
            lines.append("")

            params = backtest_result.get("best_params", backtest_result.get("custom_params", {}))

            lines.append("### 回测参数")
            lines.append("")
            lines.append("| 参数 | 值 |")
            lines.append("|---|---|")
            lines.append(f"| 右侧回撤阈值 | {params.get('trail_threshold', 0.05)*100:.0f}% |")
            lines.append(f"| 买入偏移量 | {params.get('offset', 0)*100:+.0f}% |")
            lines.append(f"| 硬止损 | {Config.STOP_LOSS_RATE*100:.0f}% |")
            lines.append(f"| 左侧止盈 | 前高90%-100%区间减持40%仓位 |")
            lines.append(f"| 右侧止盈 | 移动回撤跟踪 |")
            lines.append("")

            # 绩效总览
            bt = backtest_result.get("backtest", {})

            lines.append("### 绩效总览")
            lines.append("")
            lines.append("| 指标 | 本策略 | 买入持有 | 超额 |")
            lines.append("|---|---|---|---|")
            lines.append(f"| 总收益率 | **{backtest_metrics.get('total_return', 0):.2f}%** | {backtest_metrics.get('buy_hold_return', 0):.2f}% | **+{backtest_metrics.get('excess_return', 0):.2f}%** |")
            lines.append(f"| 最大回撤 | {backtest_metrics.get('max_drawdown', 0):.2f}% | — | — |")
            lines.append(f"| 夏普比率 | {backtest_metrics.get('sharpe_ratio', 0):.2f} | — | — |")
            lines.append(f"| Calmar 比率 | {backtest_metrics.get('calmar_ratio', 0):.2f} | — | — |")
            lines.append(f"| 胜率 | {backtest_metrics.get('win_rate', 0):.1f}% | — | — |")
            lines.append(f"| 交易次数 | {backtest_metrics.get('trade_count', 0)} 次 | — | — |")
            lines.append(f"| 最终资产 | {backtest_metrics.get('final_asset', 0):,.2f} 元 | — | — |")
            lines.append("")

            # Top 参数组合
            top_combos = backtest_result.get("top_combos", [])
            if top_combos:
                lines.append("### 最优参数 Top 5")
                lines.append("")
                lines.append("| 排名 | 右侧回撤阈值 | 买入偏移量 | 收益率 |")
                lines.append("|---|---|---|---|")
                for i, c in enumerate(top_combos, 1):
                    lines.append(f"| {i} | {c['trail_threshold']*100:.0f}% | {c['offset']*100:+.0f}% | **{c['return']:.2f}%** |")
                lines.append("")

            # 交易明细（限20条）
            trades = bt.get("trades", [])
            if trades:
                lines.append("### 交易明细")
                lines.append("")
                lines.append("| 日期 | 类型 | 价格 | 数量 | 盈亏 | 操作说明 |")
                lines.append("|---|---|---|---|---|---|")
                for t in trades[:30]:  # 最多展示30条
                    pnl = t.get("pnl", "")
                    pnl_str = f"{pnl:+.2f}" if isinstance(pnl, (int, float)) else "—"
                    reason = t.get("reason", "")
                    price = f"成交 {t['price']} / 触发 {t['trigger_price']}" if t.get("trigger_price") else t["price"]
                    lines.append(f"| {t['date']} | {t['type']} | {price} | {t.get('shares', '—')} | {pnl_str} | {reason} |")
                if len(trades) > 30:
                    lines.append(f"| ... | 共 {len(trades)} 笔交易，仅展示前30笔 | ... | ... | ... |")
                lines.append("")

            # 图表引用
            if chart_backtest_path:
                rel_path = Path(chart_backtest_path).name
                lines.append(f"![回测图]({rel_path})")
                lines.append("")

        # ── 七、K线图 ──
        if chart_kline_path:
            lines.append(f"![K线图]({Path(chart_kline_path).name})")
            lines.append("")

        # ── 八、LLM 分析 ──
        if llm_analysis:
            lines.append("## 八、LLM 智能分析")
            lines.append("")
            lines.append(llm_analysis)
            lines.append("")
        elif llm_prompt_path:
            lines.append(f"## 八、LLM 分析")
            lines.append("")
            lines.append(f"> 已生成分析 Prompt 文件：`{llm_prompt_path}`")
            lines.append("")
            lines.append("> 将此文件内容发送给 AI 助手获取分析结论。")
            lines.append("")

        # ── 免责 ──
        lines.append("---")
        lines.append("")
        lines.append("*本报告由 StockInvestmentTool 自动生成，仅供参考，不构成投资建议。*")
        lines.append("*股市有风险，入市需谨慎。*")

        report = "\n".join(lines)

        # 写入文件
        safe_name = stock_code.replace(".", "_").replace("/", "_")
        filepath = self.output_dir / f"{safe_name}_report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
        filepath.write_text(report, encoding="utf-8")
        logger.info("报告已生成: %s", filepath)

        return str(filepath)
