# -*- coding: utf-8 -*-
"""收益分析 — 持仓/自选累计收益率与收益金额

- 持仓：从建仓次日起，收盘价 vs 成本价（成本用「累计净投入/当前份额」口径）
    · 累计收益率 = (收盘价 - 成本价) / 成本价 * 100
    · 累计收益金额 = (收盘价 - 成本价) * 当前份额
- 自选/观察：从「观察起点(added_time)」次日起，以起点收盘价为基准
    · 累计收益率 = (收盘价 - 起点价) / 起点价 * 100（无份额 → 无金额）

产出: 每日明细 DataFrame + matplotlib 折线图 PNG + xlsx 导出。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

from StockInvestmentTool.config import Config

logger = logging.getLogger(__name__)


def _start_next_day(kline: pd.DataFrame, start_date: str) -> str:
    """返回 start_date 之后（含）首个交易日；start_date 早于首个交易日则用首个交易日。"""
    dates = pd.to_datetime(kline["date"])
    if start_date:
        sd = pd.to_datetime(start_date)
        after = dates[dates >= sd]
        return after.iloc[0].strftime("%Y-%m-%d") if len(after) else ""
    return kline["date"].iloc[0]


def compute_returns(kline: pd.DataFrame, start_date: str = "",
                    cost_price: Optional[float] = None,
                    shares: float = 0.0) -> pd.DataFrame:
    """计算自 start_date 次日起的每日累计收益率/金额。

    Args:
        kline: 含 date/close 列的日线（需按日期升序）。
        start_date: 观察起点/建仓日；为 '' 则从全量首日算。
        cost_price: 成本价（持仓用累计净投入/份额；自选传 None 以起点价为基准）。
        shares: 当前持仓份额（仅持仓需要金额）。

    Returns:
        DataFrame: date, close, ret_pct, ret_amount（从起点次日开始）。
    """
    df = kline.sort_values("date").reset_index(drop=True).copy()
    if df.empty:
        return df

    sd = _start_next_day(df, start_date) if start_date else df["date"].iloc[0]
    df = df[df["date"] >= sd].reset_index(drop=True)
    if df.empty:
        return df

    base = cost_price if cost_price is not None else float(df["close"].iloc[0])
    if not base:
        base = 1.0
    df["ret_pct"] = (df["close"] - base) / base * 100.0
    df["ret_amount"] = (df["close"] - base) * shares if cost_price is not None else 0.0
    return df


def build_chart(returns: pd.DataFrame, title: str,
                out_dir: Optional[Path] = None,
                filename: Optional[str] = None) -> Optional[str]:
    """生成累计收益率折线图 PNG，返回文件路径。"""
    if returns is None or returns.empty:
        return None
    try:
        from StockInvestmentTool.analysis.charts import setup_cjk_font
        setup_cjk_font()
    except Exception:
        pass

    out_dir = Path(out_dir) if out_dir else Config.CHART_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    if filename is None:
        filename = f"returns_{datetime.now():%Y%m%d%H%M%S}.png"
    path = out_dir / filename

    fig, ax = plt.subplots(figsize=(8, 3.6), dpi=110)
    x = pd.to_datetime(returns["date"])
    ax.plot(x, returns["ret_pct"], color="#1a73e8", linewidth=1.5)
    ax.axhline(0, color="#c62828", linewidth=0.8, linestyle="--")
    ax.fill_between(x, returns["ret_pct"], 0,
                    where=returns["ret_pct"] >= 0, color="#28a745", alpha=0.25)
    ax.fill_between(x, returns["ret_pct"], 0,
                    where=returns["ret_pct"] < 0, color="#dc3545", alpha=0.25)
    ax.set_title(title, fontsize=12)
    ax.set_ylabel("累计收益率(%)", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.tick_params(labelsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(str(path), bbox_inches="tight")
    plt.close(fig)
    return str(path)


def export_returns_xlsx(entries: list[dict], path: Optional[str | Path] = None) -> str:
    """导出多只标的的收益明细 + 汇总到 xlsx。

    entries: [{"code","name","kind","cost_price","shares","returns":DataFrame, ...}]
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    path = Path(path) if path else Config.DATA_DIR / f"收益分析_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = openpyxl.Workbook()
    # ── 汇总 sheet ──
    ws = wb.active
    ws.title = "汇总"
    ws.append(["代码", "名称", "类型", "起点", "成本价/基准价", "份额",
               "最新收盘", "累计收益率(%)", "累计收益金额"])
    for c, _ in enumerate([1] * 9, 1):
        ws.cell(1, c).font = Font(bold=True)
        ws.cell(1, c).fill = PatternFill("solid", fgColor="DDEBF7")
    for e in entries:
        r = e["returns"]
        last = r.iloc[-1] if (r is not None and not r.empty) else {}
        ws.append([
            e.get("code"), e.get("name"),
            "持仓" if e.get("kind") == "position" else "自选",
            e.get("start_date") or "",
            round(e.get("cost_price") or 0, 3),
            e.get("shares") or 0,
            round(float(last.get("close", 0)), 2) if (r is not None and not r.empty) else "",
            round(float(last.get("ret_pct", 0)), 2) if (r is not None and not r.empty) else "",
            round(float(last.get("ret_amount", 0)), 2) if (r is not None and not r.empty) else "",
        ])
    widths = [14, 12, 8, 12, 12, 12, 12, 14, 14]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    # ── 每只标的明细 sheet ──
    for e in entries:
        r = e["returns"]
        if r is None or r.empty:
            continue
        name = e.get("name") or e.get("code")
        wsn = f"{name[:12]}_{e.get('code','')[-6:]}"
        wsn = "".join(ch for ch in wsn if ch not in '[]:*?/\\')
        ws2 = wb.create_sheet(wsn[:31])
        ws2.append(["日期", "收盘价", "累计收益率(%)", "累计收益金额"])
        for c in range(1, 5):
            ws2.cell(1, c).font = Font(bold=True)
            ws2.cell(1, c).fill = PatternFill("solid", fgColor="E3F2FD")
        for _, row in r.iterrows():
            ws2.append([
                row["date"], round(float(row["close"]), 2),
                round(float(row["ret_pct"]), 2),
                round(float(row["ret_amount"]), 2),
            ])
        for i, w in enumerate([12, 12, 16, 16], 1):
            ws2.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    wb.save(str(path))
    logger.info("收益分析已导出: %s", path)
    return str(path)


def build_snapshot_chart(kline, stock_name: str, code: str,
                         out_dir: Optional[Path] = None,
                         filename: Optional[str] = None) -> Optional[str]:
    """生成单只股票的快照图（收盘价 + MA20/MA60，风格与页面 ECharts 一致）。

    用于邮件内嵌：比纯收益图信息更丰富，用户一眼看到当前走势/均线位置。
    """
    if kline is None or kline.empty:
        return None
    try:
        from StockInvestmentTool.analysis.charts import setup_cjk_font
        setup_cjk_font()
    except Exception:
        pass

    out_dir = Path(out_dir) if out_dir else Config.CHART_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    if filename is None:
        filename = f"snap_{code}_{datetime.now():%Y%m%d%H%M%S}.png"
    path = out_dir / filename

    df = kline.copy()
    df = df.sort_values("date")
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df = df.tail(120)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=120)
    ax.plot(df["date"], df["close"], color="#1a73e8", linewidth=1.6,
            label="收盘")
    ax.plot(df["date"], df["ma20"], color="#e67e22", linewidth=1.0,
            label="MA20")
    ax.plot(df["date"], df["ma60"], color="#27ae60", linewidth=1.0,
            label="MA60")
    last = df.iloc[-1]
    ax.axhline(last["close"], color="#1a73e8", linewidth=0.8, linestyle="--", alpha=0.5)
    ax.set_title(f"{stock_name}（{code}） 最新 {last['close']:.2f}", fontsize=13)
    ax.set_ylabel("价格", fontsize=10)
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best", fontsize=9)
    ax.tick_params(labelsize=9)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(str(path), bbox_inches="tight")
    plt.close(fig)
    return str(path)
