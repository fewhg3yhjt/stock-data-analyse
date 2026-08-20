"""Excel 导入导出 — 与网页版 my_portfolio.xlsx 格式对齐

导出格式:
  Sheet "关注列表": 代码 | 名称 | 类型 | 计划总仓位(元) | 弱支撑 | 强支撑 | 极端低估锚 | 备注
  Sheet "持仓记录": 代码 | 买入日期 | 买入均价 | 持有份额 | 当前成本 | 上次操作日期 | 方案 | 备注

导入:
  读取 "关注列表" → 写入 watchlist
  读取 "持仓记录" → 创建 position（若已存在同代码 open 持仓则跳过/提示）
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from StockInvestmentTool.config import Config
from StockInvestmentTool.portfolio.manager import PortfolioManager
from StockInvestmentTool.portfolio.models import Position, WatchlistItem

logger = logging.getLogger(__name__)

# 列定义（与网页版格式一致）
WATCHLIST_HEADERS = ["代码", "名称", "类型", "计划总仓位(元)", "弱支撑", "强支撑", "极端低估锚", "备注"]
POSITION_HEADERS = ["代码", "名称", "买入日期", "买入均价", "持有份额", "当前成本", "方案", "备注"]

_TXN_CN = {"buy": "买入", "sell": "卖出", "sell_all": "清仓", "dividend": "分红", "correction": "纠错"}


def review_to_excel(data: dict, path: Optional[str | Path] = None) -> str:
    """导出复盘：Sheet「统计看板」+ Sheet「全量交易流水（FIFO 配对）」。

    data: DashboardService.review_ledger() 的返回（含 stats / txns / total）。
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    if path is None:
        path = Config.DATA_DIR / f"复盘_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = openpyxl.Workbook()

    # ── 统计看板 ──
    ws = wb.active
    ws.title = "统计看板"
    stats = data.get("stats") or {}
    rows = [
        ("已平仓笔数", stats.get("closed_trades")),
        ("胜率(%)", stats.get("win_rate")),
        ("平均盈利(元)", stats.get("avg_win")),
        ("平均亏损(元)", stats.get("avg_loss")),
        ("盈亏比(目标>2:1)", stats.get("pnl_ratio")),
        ("平均持仓天数", stats.get("avg_holding_days")),
        ("样本置信度", stats.get("confidence")),
    ]
    for i, (k, v) in enumerate(rows, 1):
        ws.cell(i, 1, k).font = Font(bold=True)
        ws.cell(i, 2, v if v is not None else "—")
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 30

    # ── 全量交易流水 ──
    ws2 = wb.create_sheet("交易流水(FIFO)")
    headers = ["日期", "标的", "代码", "方向", "价格", "数量", "金额", "手续费", "盈亏", "备注"]
    ws2.append(headers)
    for c, h in enumerate(headers, 1):
        ws2.cell(1, c).font = Font(bold=True)
        ws2.cell(1, c).fill = PatternFill("solid", fgColor="E3F2FD")
    for t in data.get("txns", []):
        ws2.append([
            t.get("date"), t.get("stock_name"), t.get("stock_code"),
            _TXN_CN.get(t.get("trans_type"), t.get("trans_type")),
            t.get("price"), t.get("shares"), t.get("amount"),
            t.get("fee"), t.get("pnl"), t.get("reason"),
        ])
    widths = [12, 14, 12, 8, 10, 10, 12, 10, 10, 20]
    for i, w in enumerate(widths, 1):
        ws2.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    wb.save(str(path))
    logger.info("复盘已导出: %s", path)
    return str(path)


def export_to_excel(manager: PortfolioManager, path: Optional[str | Path] = None) -> str:
    """导出持仓与自选池到 Excel

    Returns:
        文件路径
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    path = Path(path) if path else Path(manager.storage.db_path).parent / "portfolio_export.xlsx"
    wb = openpyxl.Workbook()

    # ── Sheet 1: 关注列表 ──
    ws1 = wb.active
    ws1.title = "关注列表"
    ws1.append(WATCHLIST_HEADERS)
    for item in manager.get_watchlist():
        ws1.append([
            item.stock_code, item.stock_name, item.asset_type,
            item.target_capital, item.weak_support or "",
            item.strong_support or "", item.extreme_anchor or "", item.notes,
        ])

    # ── Sheet 2: 持仓记录 ──
    ws2 = wb.create_sheet("持仓记录")
    ws2.append(POSITION_HEADERS)
    for p in manager.storage.get_positions():
        if p.status != "open":
            continue
        ws2.append([
            p.stock_code, p.stock_name, p.buy_date, p.avg_cost,
            p.total_shares, p.total_cost, p.scheme_name, p.notes,
        ])

    # 表头样式
    header_font = Font(bold=True)
    header_fill = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")
    for ws in (ws1, ws2):
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill

    # 列宽
    for ws, widths in ((ws1, [14, 12, 8, 14, 10, 10, 12, 20]),
                       (ws2, [14, 12, 12, 10, 10, 12, 16, 20])):
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    wb.save(str(path))
    logger.info("Excel 已导出: %s", path)
    return str(path)


def import_from_excel(manager: PortfolioManager, path: str | Path,
                      import_positions: bool = True) -> dict:
    """从 Excel 导入自选池与持仓

    Returns:
        {"watchlist": 导入条数, "positions": 导入条数, "skipped": 跳过明细}
    """
    import openpyxl

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Excel 文件不存在: {path}")
    wb = openpyxl.load_workbook(str(path), data_only=True)

    result = {"watchlist": 0, "positions": 0, "skipped": []}

    # ── 关注列表 ──
    if "关注列表" in wb.sheetnames:
        ws = wb["关注列表"]
        headers = [c.value for c in ws[1]]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not row[0]:
                continue
            code = str(row[0]).strip()
            name = str(row[1] or "").strip() if len(row) > 1 else ""
            try:
                item = WatchlistItem(
                    stock_code=code, stock_name=name,
                    asset_type=str(row[2] or "stock").strip() if len(row) > 2 else "stock",
                    target_capital=float(row[3] or 0) if len(row) > 3 else 0,
                    weak_support=float(row[4] or 0) if len(row) > 4 else 0,
                    strong_support=float(row[5] or 0) if len(row) > 5 else 0,
                    extreme_anchor=float(row[6] or 0) if len(row) > 6 else 0,
                    notes=str(row[7] or "") if len(row) > 7 else "",
                )
                manager.storage.add_watchlist(item)
                result["watchlist"] += 1
            except Exception as e:
                result["skipped"].append(f"自选池 {code}: {e}")

    # ── 持仓记录 ──
    if import_positions and "持仓记录" in wb.sheetnames:
        ws = wb["持仓记录"]
        existing_codes = {p.stock_code for p in manager.storage.get_positions()}
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not row[0]:
                continue
            code = str(row[0]).strip()
            name = str(row[1] or "").strip() if len(row) > 1 else ""
            try:
                if code in existing_codes:
                    result["skipped"].append(f"持仓 {code}: 已存在，跳过")
                    continue
                buy_date = str(row[2] or "")[:10] if len(row) > 2 else ""
                avg_cost = float(row[3]) if len(row) > 3 and row[3] else 0
                shares = float(row[4]) if len(row) > 4 and row[4] else 0
                scheme = str(row[6] or "default_value").strip() if len(row) > 6 else "default_value"
                notes = str(row[7] or "") if len(row) > 7 else ""
                if avg_cost <= 0 or shares <= 0:
                    result["skipped"].append(f"持仓 {code}: 份额/成本无效，跳过")
                    continue
                manager.add_position(
                    stock_code=code, stock_name=name or code,
                    shares=shares, cost=avg_cost, buy_date=buy_date or "2026-01-01",
                    scheme_name=scheme, notes=notes,
                )
                existing_codes.add(code)
                result["positions"] += 1
            except Exception as e:
                result["skipped"].append(f"持仓 {code}: {e}")

    logger.info("Excel 导入完成: %s", result)
    return result
