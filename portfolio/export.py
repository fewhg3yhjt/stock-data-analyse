"""Excel 导入导出 — 统一多 sheet 模板（自选 / 持仓 / 加仓 / 操作笔记）

导出:  4 个 sheet，加仓 sheet 留空供填写，操作笔记 sheet 为当前全量交易流水。
导入:  有数据的 sheet 就解析、缺 sheet 或空行跳过。
       加仓对已有持仓追加 buy，无持仓则自动建仓；笔记追加到匹配交易/持仓备注。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from StockInvestmentTool.config import Config
from StockInvestmentTool.portfolio.manager import PortfolioManager
from StockInvestmentTool.portfolio.models import Position, WatchlistItem, TXN_BUY

logger = logging.getLogger(__name__)

# 列定义（与网页版格式一致）
# 统一模板多 sheet：自选 / 持仓 / 加仓 / 操作笔记
WATCHLIST_HEADERS = ["代码", "名称", "类型", "计划总仓位(元)", "弱支撑", "强支撑", "极端低估锚", "备注"]
POSITION_HEADERS = ["代码", "名称", "买入日期", "买入均价", "持有份额", "当前成本", "方案", "备注"]
TOPUP_HEADERS = ["代码", "名称", "买入日期", "买入价格", "数量", "备注"]
NOTE_HEADERS = ["日期", "代码", "名称", "方向", "价格", "数量", "备注"]

SHEET_WATCHLIST = "自选"
SHEET_POSITION = "持仓"
SHEET_TOPUP = "加仓"
SHEET_NOTE = "操作笔记"

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
    """导出到统一 xlsx 模板（自选 / 持仓 / 加仓 / 操作笔记 多 sheet）

    加仓 sheet 默认留空（供手动填写批量加仓），操作笔记 sheet 为当前全量交易流水。
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    path = Path(path) if path else Path(manager.storage.db_path).parent / "portfolio_template.xlsx"
    wb = openpyxl.Workbook()

    def _style(ws: "openpyxl.worksheet.worksheet.Worksheet", widths: list[int]):
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    # ── Sheet 1: 自选 ──
    ws1 = wb.active
    ws1.title = SHEET_WATCHLIST
    ws1.append(WATCHLIST_HEADERS)
    for item in manager.get_watchlist():
        ws1.append([
            item.stock_code, item.stock_name, item.asset_type,
            item.target_capital, item.weak_support or "",
            item.strong_support or "", item.extreme_anchor or "", item.notes,
        ])
    _style(ws1, [14, 12, 8, 14, 10, 10, 12, 20])

    # ── Sheet 2: 持仓 ──
    ws2 = wb.create_sheet(SHEET_POSITION)
    ws2.append(POSITION_HEADERS)
    for p in manager.storage.get_positions():
        if p.status != "open":
            continue
        ws2.append([
            p.stock_code, p.stock_name, p.buy_date, p.avg_cost,
            p.total_shares, p.total_cost, p.scheme_name, p.notes,
        ])
    _style(ws2, [14, 12, 12, 10, 10, 12, 16, 20])

    # ── Sheet 3: 加仓（留空模板，供批量填写）──
    ws3 = wb.create_sheet(SHEET_TOPUP)
    ws3.append(TOPUP_HEADERS)
    _style(ws3, [14, 12, 12, 10, 10, 20])

    # ── Sheet 4: 操作笔记（当前全量交易流水）──
    ws4 = wb.create_sheet(SHEET_NOTE)
    ws4.append(NOTE_HEADERS)
    for t in manager.list_transactions():
        ws4.append([
            t.get("date"), t.get("stock_code"), t.get("stock_name"),
            _TXN_CN.get(t.get("trans_type"), t.get("trans_type")),
            t.get("price"), t.get("shares"), t.get("reason"),
        ])
    _style(ws4, [12, 14, 12, 8, 10, 10, 30])

    wb.save(str(path))
    logger.info("Excel 已导出: %s", path)
    return str(path)


def import_from_excel(manager: PortfolioManager, path: str | Path,
                      import_positions: bool = True) -> dict:
    """从统一 xlsx 模板导入（自选 / 持仓 / 加仓 / 操作笔记）

    有数据的 sheet 就解析，缺 sheet 或空行则跳过。
    加仓：对已有 open 持仓追加 buy 交易；若无持仓则自动建仓。
    操作笔记：仅当对应方向/标的可定位到交易时追加备注（reason）。

    Returns:
        {"watchlist": n, "positions": n, "topups": n, "notes": n, "skipped": [...]}
    """
    import openpyxl

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Excel 文件不存在: {path}")
    wb = openpyxl.load_workbook(str(path), data_only=True)

    result = {"watchlist": 0, "positions": 0, "topups": 0, "notes": 0, "skipped": []}
    existing_codes = {p.stock_code for p in manager.storage.get_positions()}

    def _find_open_position(code: str):
        from StockInvestmentTool.data.fetcher import StockDataFetcher
        norm = StockDataFetcher.normalize_code(code)
        for p in manager.storage.get_open_positions():
            if StockDataFetcher.normalize_code(p.stock_code) == norm:
                return p
        return None

    # ── 自选 ──
    if SHEET_WATCHLIST in wb.sheetnames:
        ws = wb[SHEET_WATCHLIST]
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
                result["skipped"].append(f"自选 {code}: {e}")

    # ── 持仓 ──
    if import_positions and SHEET_POSITION in wb.sheetnames:
        ws = wb[SHEET_POSITION]
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

    # ── 加仓 ──
    if SHEET_TOPUP in wb.sheetnames:
        ws = wb[SHEET_TOPUP]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not row[0]:
                continue
            code = str(row[0]).strip()
            name = str(row[1] or "").strip() if len(row) > 1 else code
            try:
                date = str(row[2] or "")[:10] if len(row) > 2 else ""
                price = float(row[3]) if len(row) > 3 and row[3] else 0
                shares = float(row[4]) if len(row) > 4 and row[4] else 0
                reason = str(row[5] or "").strip() if len(row) > 5 else "批量加仓"
                if price <= 0 or shares <= 0:
                    result["skipped"].append(f"加仓 {code}: 价格/数量无效，跳过")
                    continue
                pos = _find_open_position(code)
                if pos is None:
                    # 无持仓 → 自动建仓
                    manager.add_position(
                        stock_code=code, stock_name=name,
                        shares=shares, cost=price,
                        buy_date=date or "2026-01-01", notes=reason,
                    )
                    existing_codes.add(code)
                    result["topups"] += 1
                else:
                    manager.record_transaction(
                        pos.id, TXN_BUY, price=price, shares=shares,
                        date=date or None, reason=reason,
                    )
                    result["topups"] += 1
            except Exception as e:
                result["skipped"].append(f"加仓 {code}: {e}")

    # ── 操作笔记（追加到匹配交易/持仓备注）──
    if SHEET_NOTE in wb.sheetnames:
        ws = wb[SHEET_NOTE]
        cn2type = {v: k for k, v in _TXN_CN.items()}
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not row[0]:
                continue
            try:
                date = str(row[0] or "")[:10]
                code = str(row[1] or "").strip()
                direction_cn = str(row[3] or "").strip() if len(row) > 3 else ""
                note = str(row[6] or "").strip() if len(row) > 6 else ""
                if not note or not code:
                    continue
                pos = _find_open_position(code)
                if pos is None:
                    result["skipped"].append(f"笔记 {code}: 无匹配持仓，跳过")
                    continue
                # 优先匹配同日期/方向交易，追加备注；否则记到持仓备注
                ttype = cn2type.get(direction_cn, "")
                matched = None
                for t in manager.storage.get_transactions(pos.id):
                    if (not date or t.date[:10] == date) and (not ttype or t.trans_type == ttype):
                        matched = t
                        break
                if matched is not None:
                    new_reason = (matched.reason + "；" + note) if matched.reason else note
                    manager.update_transaction_note(matched.id, new_reason)
                else:
                    new_notes = (pos.notes + "；" + note) if pos.notes else note
                    manager.update_position_note(pos.id, new_notes)
                result["notes"] += 1
            except Exception as e:
                result["skipped"].append(f"笔记: {e}")

    logger.info("Excel 导入完成: %s", result)
    return result
