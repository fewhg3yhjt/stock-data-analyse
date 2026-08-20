# -*- coding: utf-8 -*-
"""资金流分析 — 大盘概况 / 趋势标签 / 背离检测 / 榜单

核心逻辑:
    - 趋势标签: 比较 今日净额 与 近N日累计净额 的符号
      持续流入(今+多+)/持续流出(今-多-)/转流入(今+多-)/转流出(今-多+)
    - 背离检测: 价格涨(涨跌幅>0) 但 主力净额<0 → 「价涨钱走」，短线警示
    - 健康配合: 价格涨 且 主力净额>0 → 「价涨钱随」，强势信号
"""

from typing import Optional

import pandas as pd

# 同花顺金额单位为「亿」；阈值用于判定资金方向
FLOW_EPS = 0.01  # 亿，净额绝对值低于此视为「无方向」


def trend_label(net_now: float, net_days: float) -> str:
    """今日 vs 多日累计 → 资金趋势标签。"""
    if net_now is None or net_days is None:
        return "数据缺失"
    if net_now > FLOW_EPS and net_days > FLOW_EPS:
        return "持续流入"
    if net_now < -FLOW_EPS and net_days < -FLOW_EPS:
        return "持续流出"
    if net_now > FLOW_EPS and net_days <= FLOW_EPS:
        return "转为流入"
    if net_now < -FLOW_EPS and net_days >= -FLOW_EPS:
        return "转为流出"
    return "无方向"


def market_overview(stock_now: pd.DataFrame) -> dict:
    """大盘概况: 涨跌家数 / 资金流向 / 背离与配合计数。"""
    if stock_now is None or stock_now.empty:
        return {}
    chg = stock_now["chg"].dropna()
    net = stock_now["net"].dropna()
    up = int((chg > 0).sum())
    dn = int((chg < 0).sum())
    flat = int((chg == 0).sum())
    both = chg.index.intersection(net.index)
    diverge = int(((chg > 0) & (net < 0)).sum())   # 价涨钱走
    follow = int(((chg > 0) & (net > 0)).sum())    # 价涨钱随
    return {
        "个股总数": len(stock_now),
        "上涨": up,
        "下跌": dn,
        "平盘": flat,
        "主力净流入家数": int((net > 0).sum()),
        "主力净流出家数": int((net < 0).sum()),
        "全市场净额合计(亿)": round(float(net.sum()), 1),
        "价涨钱走(背离)": diverge,
        "价涨钱随(配合)": follow,
    }


def merge_trend(now_df: pd.DataFrame, days_df: Optional[pd.DataFrame],
                on: str) -> pd.DataFrame:
    """合并 即时 与 多日累计 两个表，附加趋势标签。

    days_df 兼容两种结构:
      - 板块类: 多日表列名仍为 net/chg（在 merge_trend 内转 net_days/chg_days）
      - 个股类: 多日表直接带 net_days/chg_days（同花顺列名不同）
    """
    if days_df is None or days_df.empty:
        out = now_df.copy()
        out["net_days"] = None
        out["chg_days"] = None
        out["trend"] = "无多日对比"
        return out
    days = days_df.copy()
    if "net" in days.columns and "net_days" not in days.columns:
        days = days.rename(columns={"net": "net_days"})
    if "chg" in days.columns and "chg_days" not in days.columns:
        days = days.rename(columns={"chg": "chg_days"})
    keep = [on, "net_days", "chg_days"]
    days = days[[c for c in keep if c in days.columns]]
    out = now_df.merge(days, on=on, how="left")
    out["trend"] = [
        trend_label(n, d) for n, d in zip(out["net"], out["net_days"])
    ]
    return out


def sector_analysis(now_df: pd.DataFrame, days_df: Optional[pd.DataFrame] = None,
                    top: int = 15) -> pd.DataFrame:
    """板块(行业/概念)资金流分析: 排行 + 多日趋势。"""
    out = merge_trend(now_df, days_df, on="name")
    out = out.sort_values("net", ascending=False, na_position="last")
    return out.head(top).reset_index(drop=True)


def stock_analysis(now_df: pd.DataFrame, days_df: Optional[pd.DataFrame] = None,
                   top: int = 15) -> dict:
    """个股资金流分析: 净流入榜 / 净流出榜 / 持续流入榜 / 背离榜。"""
    base = merge_trend(now_df, days_df, on="code")
    top_in = base.sort_values("net", ascending=False, na_position="last").head(top)
    top_out = base.sort_values("net", ascending=True, na_position="last").head(top)
    sustained = base[(base["net"] > FLOW_EPS) & (base["net_days"] > FLOW_EPS)]
    sustained = sustained.sort_values("net_days", ascending=False).head(top)
    diverge = base[(base["chg"] > 0) & (base["net"] < 0)]
    diverge = diverge.sort_values("net").head(top)
    return {
        "净流入榜": top_in.reset_index(drop=True),
        "净流出榜": top_out.reset_index(drop=True),
        "持续流入榜": sustained.reset_index(drop=True),
        "价涨钱走(背离)榜": diverge.reset_index(drop=True),
    }
