"""晨报生成 — 4 张表 Markdown 快照输出

  表1: 大盘温度计   — 主要指数涨跌 + 市场状态
  表2: 关注列表操作指导 — 自选池（未持仓）距支撑位
  表3: 已持仓操作指导 — 持仓浮动盈亏 + 今日建议
  表4: 今日待执行指令汇总 — 可执行建议清单
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from StockInvestmentTool.config import Config
from StockInvestmentTool.datasource.fetcher import StockDataFetcher
from StockInvestmentTool.strategy.market_state import determine_market_state_from_df
from StockInvestmentTool.portfolio.manager import PortfolioManager
from StockInvestmentTool.portfolio.models import (
    ADVICE_BUY_MORE, ADVICE_PARTIAL_SELL, ADVICE_SELL_ALL, ADVICE_HOLD,
)

logger = logging.getLogger(__name__)

# 大盘温度计跟踪的指数
MARKET_INDICES = [
    ("沪深300", "sh.000300"),
    ("上证指数", "sh.000001"),
    ("深证成指", "sz.399001"),
    ("科创50", "sh.000688"),
]

# 建议类型 → 可读标签
ADVICE_LABELS = {
    ADVICE_BUY_MORE: "加仓",
    ADVICE_PARTIAL_SELL: "减仓",
    ADVICE_SELL_ALL: "清仓",
    ADVICE_HOLD: "持有",
}


class MorningReporter:
    """晨报生成器"""

    def __init__(self, manager: Optional[PortfolioManager] = None,
                 output_dir: Optional[Path] = None):
        self.manager = manager or PortfolioManager()
        self.output_dir = Path(output_dir) if output_dir else Config.REPORT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── 表1: 大盘温度计 ───────────────────────────────

    def _market_table(self) -> list[dict]:
        """计算主要指数涨跌与市场状态"""
        rows = []
        with StockDataFetcher() as fetcher:
            end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
            for name, code in MARKET_INDICES:
                try:
                    kline = fetcher.get_kline(code, start_date, end_date)
                    if len(kline) < 5:
                        continue
                    last = kline.iloc[-1]
                    prev = kline.iloc[-2]
                    change_pct = (last["close"] / prev["close"] - 1) * 100
                    recent_5 = kline["close"].tail(5)
                    pct_5 = (recent_5.iloc[-1] / recent_5.iloc[0] - 1) * 100
                    state = determine_market_state_from_df(kline)
                    rows.append({
                        "name": name, "code": code,
                        "close": round(float(last["close"]), 3),
                        "change_pct": round(change_pct, 2),
                        "pct_5d": round(pct_5, 2),
                        "state": state,
                    })
                except Exception as e:
                    logger.warning("指数 %s 获取失败: %s", code, e)
        return rows

    # ── 表2: 关注列表 ─────────────────────────────────

    def _watchlist_table(self) -> list[dict]:
        """自选池（未持仓）当前价距支撑位"""
        rows = []
        open_codes = {p.stock_code for p in self.manager.storage.get_open_positions()}
        from StockInvestmentTool.portfolio.monitor import PriceMonitor
        monitor = PriceMonitor()
        for item in self.manager.get_watchlist():
            if item.stock_code in open_codes:
                continue  # 已持仓的在表3
            try:
                # warehouse 优先，baostock 兜底（含技术指标）
                kline = monitor.fetch_kline(item.stock_code)
                last = kline.iloc[-1]
                current = float(last["close"])
                weak = item.weak_support
                # 未手动预设时用 MA60 近似
                if weak <= 0 and "ma_60" in kline.columns:
                    weak = float(last.get("ma_60", 0))
                dist = (current / weak - 1) * 100 if weak > 0 else None
                rows.append({
                    "code": item.stock_code, "name": item.stock_name,
                    "current": round(current, 3),
                    "weak_support": round(weak, 3) if weak > 0 else None,
                    "distance_pct": round(dist, 2) if dist is not None else None,
                    "target_capital": item.target_capital,
                    "action": f"挂单{round(weak,2)}等待" if weak > 0 and current > weak else "已到位，可买入",
                    "priority": "高" if current <= weak else "中",
                })
            except Exception as e:
                    logger.warning("自选 %s 分析失败: %s", item.stock_code, e)
        return rows

    # ── 表3: 已持仓 ───────────────────────────────────

    def _position_table(self) -> list[dict]:
        """持仓 + 最新建议"""
        rows = []
        for p in self.manager.storage.get_open_positions():
            advice = self.manager.storage.get_latest_advice(p.id)
            rows.append({
                "code": p.stock_code, "name": p.stock_name,
                "cost": p.avg_cost, "current": p.current_price,
                "pnl_pct": p.unrealized_pnl_pct,
                "shares": p.total_shares,
                "total_cost": p.total_cost,
                "market_value": p.market_value,
                "advice_type": advice.advice_type if advice else ADVICE_HOLD,
                "advice_label": ADVICE_LABELS.get(advice.advice_type if advice else ADVICE_HOLD, "持有"),
                "reason": advice.reason if advice else "",
                "suggested_amount": advice.suggested_amount if advice else 0,
            })
        return rows

    # ── 表4: 今日指令 ─────────────────────────────────

    def _order_table(self, positions: list[dict]) -> list[dict]:
        """可执行建议汇总"""
        orders = []
        for p in positions:
            if p["advice_type"] == ADVICE_SELL_ALL:
                orders.append({"type": "卖出", "code": p["code"], "name": p["name"],
                               "action": "市价清仓", "price": p["current"],
                               "amount": p["market_value"], "reason": p["reason"]})
            elif p["advice_type"] == ADVICE_PARTIAL_SELL:
                orders.append({"type": "卖出", "code": p["code"], "name": p["name"],
                               "action": "减仓止盈", "price": p["current"],
                               "amount": p["suggested_amount"], "reason": p["reason"]})
            elif p["advice_type"] == ADVICE_BUY_MORE:
                orders.append({"type": "买入", "code": p["code"], "name": p["name"],
                               "action": "加仓", "price": p["current"],
                               "amount": p["suggested_amount"], "reason": p["reason"]})
        return orders

    # ── 主入口 ────────────────────────────────────────

    def generate(self, refresh: bool = False) -> str:
        """生成晨报

        Args:
            refresh: 是否先刷新持仓价格

        Returns:
            晨报文件路径
        """
        if refresh:
            self.manager.refresh_all()

        today = datetime.now().strftime("%Y-%m-%d")
        lines: list[str] = []
        lines.append(f"# 每日晨报 {today}")
        lines.append("")
        lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
        lines.append("")

        # ── 表1: 大盘温度计 ──
        lines.append("## 一、大盘温度计")
        lines.append("")
        lines.append("| 指数 | 昨收 | 涨跌幅 | 近5日 | 市场状态 |")
        lines.append("|---|---|---|---|---|")
        market_rows = self._market_table()
        for r in market_rows:
            lines.append(f"| {r['name']} | {r['close']} | {r['change_pct']:+.2f}% | "
                         f"{r['pct_5d']:+.2f}% | {r['state']} |")
        if not market_rows:
            lines.append("| — | 数据获取失败 | — | — | — |")
        lines.append("")

        # ── 表2: 关注列表 ──
        lines.append("## 二、关注列表操作指导")
        lines.append("")
        lines.append("| 代码 | 名称 | 当前价 | 弱支撑 | 距弱支撑 | 操作建议 | 优先级 |")
        lines.append("|---|---|---|---|---|---|---|")
        watch_rows = self._watchlist_table()
        for r in watch_rows:
            dist = f"{r['distance_pct']:+.2f}%" if r["distance_pct"] is not None else "—"
            weak = f"{r['weak_support']}" if r["weak_support"] else "—"
            lines.append(f"| {r['code']} | {r['name']} | {r['current']} | {weak} | "
                         f"{dist} | {r['action']} | {r['priority']} |")
        if not watch_rows:
            lines.append("| — | 自选池为空 | — | — | — | — | — |")
        lines.append("")

        # ── 表3: 已持仓 ──
        lines.append("## 三、已持仓操作指导")
        lines.append("")
        lines.append("| 代码 | 名称 | 持仓成本 | 当前价 | 浮动盈亏 | 今日操作 | 建议金额 | 备注 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        pos_rows = self._position_table()
        for r in pos_rows:
            amount = f"{r['suggested_amount']:,.0f}" if r["suggested_amount"] else "0"
            note = r["reason"][:40] if r["reason"] else ""
            lines.append(f"| {r['code']} | {r['name']} | {r['cost']:.2f} | {r['current']:.2f} | "
                         f"{r['pnl_pct']:+.2f}% | **{r['advice_label']}** | {amount} | {note} |")
        if not pos_rows:
            lines.append("| — | 暂无持仓 | — | — | — | — | — | — |")
        lines.append("")

        # ── 表4: 今日指令 ──
        lines.append("## 四、今日待执行指令汇总")
        lines.append("")
        orders = self._order_table(pos_rows)
        lines.append("| 类型 | 代码 | 名称 | 操作 | 价格 | 金额 | 说明 |")
        lines.append("|---|---|---|---|---|---|---|")
        for o in orders:
            lines.append(f"| {o['type']} | {o['code']} | {o['name']} | {o['action']} | "
                         f"{o['price']} | {o['amount']:,.0f} | {o['reason'][:50]} |")
        if not orders:
            lines.append("| — | 今日无操作指令 | — | — | — | — | 全部持有观望 |")
        lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("*本晨报由 StockInvestmentTool 自动生成，仅供参考，不构成投资建议。*")

        path = self.output_dir / f"晨报_{today}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("晨报已生成: %s", path)
        return str(path)
