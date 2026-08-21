# -*- coding: utf-8 -*-
"""在线快照采集 — 观察池盘中低频更新（腾讯报价，不封 IP）

设计:
  - 只处理「纳入观察的股票」（默认来自 portfolio 观察池/自选/持仓），
    绝不做全市场盘中快照（2C2G 下全市场盘中会拖垮存储与带宽）
  - 腾讯批量报价一次 ~60 只，低频（每 10-30 分钟）足够盘中信号
  - 落盘到 warehouse/online/YYYY-MM-DD/snapshot_HHMMSS.csv（按日滚动）
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def _default_observe_codes() -> list[str]:
    """默认观察代码集：持仓 + 自选（失败降级空列表）。"""
    try:
        from StockInvestmentTool.portfolio.manager import PortfolioManager
        mgr = PortfolioManager()
        codes = set()
        for p in mgr.storage.get_open_positions():
            codes.add(p.stock_code)
        codes.update(mgr.get_watchlist_codes())
        return sorted(codes)
    except Exception as e:
        logger.warning("获取观察代码失败: %s", e)
        return []


def _normalize_codes(codes: list[str]) -> list[str]:
    """代码统一为腾讯格式（sh600900 无点）。"""
    from StockInvestmentTool.screener.board import normalize
    out = []
    for c in codes:
        c = (c or "").strip().lower().replace(".", "")
        if not c:
            continue
        if c.startswith(("sh", "sz", "bj")):
            out.append(c)
        else:
            n = normalize(c)
            out.append(n)
    return out


def collect_online_snapshot(codes: Optional[list[str]] = None,
                            day: Optional[str] = None) -> str:
    """采集一次在线快照，写入按日目录。返回文件路径。

    Args:
        codes: 代码列表（None=持仓+自选）
        day: YYYY-MM-DD（默认今天）
    """
    from StockInvestmentTool.screener.sources import tencent_quotes
    from StockInvestmentTool.warehouse.storage import Warehouse

    if codes is None:
        codes = _default_observe_codes()
    codes = _normalize_codes(codes)
    if not codes:
        raise ValueError("无观察代码（请先加持仓/自选，或显式传 --codes）")

    w = Warehouse()
    day = day or datetime.now().strftime("%Y-%m-%d")
    quotes = tencent_quotes(codes)
    if quotes is None or quotes.empty:
        raise RuntimeError("腾讯报价返回空")

    # 保留增强字段 + 标记僵尸报价
    keep = [c for c in ("code", "name", "price", "prev_close", "open",
                        "high", "low", "change_pct", "turnover", "vol_ratio",
                        "pe_ttm", "pb", "total_mcap", "float_mcap",
                        "amount_wan", "is_stale", "stale_reason") if c in quotes.columns]
    df = quotes[keep].copy()
    df["snapshot_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return str(w.write_online_snapshot(day, df))