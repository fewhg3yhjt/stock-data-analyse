# -*- coding: utf-8 -*-
"""策略实验室 — 快速验证选股策略（不固化进主分析流程）

设计:
  - 条件可配（CONDITIONS dict），改参数即可重新验证
  - 用 DuckDB 窗口函数直接对 warehouse parquet 计算，2C2G 可跑
  - 扫描: 找当前/历史某时点符合条件的所有股票
  - 回测: 历史每个时点选股 → 持有 N 天收益，与全市场基准对比
  - 图表: 折线图写入 Config.CHART_DIR，供 web /charts/ 展示

目的: 快速判断策略是否有效，有效才考虑正式固化。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd

from StockInvestmentTool.config import Config
from StockInvestmentTool.warehouse.storage import Warehouse

logger = logging.getLogger(__name__)

# ── 默认策略条件（可配，web 页面可调）────────────────
DEFAULT_CONDITIONS = {
    "above_ma60": True,           # 股价 > 60日线
    "limit_up_10d": 1,            # 最近10日涨停次数 >= 1
    "limit_up_pct": 0.098,        # 主板涨停阈值
    "deviation_ma20_max": 0.10,   # 偏离20日线 < 10%
    "min_history": 80,
}

GROWTH_PREFIX = ("300", "301", "688", "689")


def _limit_pct_sql(limit_up_pct: float) -> str:
    """创业板/科创板涨停 20%，主板 10% 的 SQL 表达式。"""
    return f"""
    CASE WHEN regexp_extract(code, '(300|301|688|689)') != '' THEN 0.198
         ELSE {limit_up_pct} END
    """


def _parquet_files(warehouse) -> str:
    files = [str(warehouse.daily_partition(m))
             for m in warehouse.available_months("daily")]
    return "[" + ",".join("'" + f + "'" for f in files) + "]"


# ═══════════════════════════════════════════════════════
# 扫描：找某时点符合条件的所有股票
# ═══════════════════════════════════════════════════════

def scan(conditions: Optional[dict] = None, as_of: str = "",
         top_n: int = 50) -> list[dict]:
    """扫描全市场，返回 as_of 时点符合条件的前 top_n 只（按偏离度升序）。"""
    c = {**DEFAULT_CONDITIONS, **(conditions or {})}
    w = Warehouse()
    fl = _parquet_files(w)
    if not as_of:
        # 默认取仓库最新交易日
        df = w.read_daily(w.available_months("daily")[-1])
        as_of = str(df["date"].max())[:10]

    con = duckdb.connect()
    try:
        limit_expr = _limit_pct_sql(c["limit_up_pct"])
        sql = f"""
        WITH daily AS (
            SELECT code, date, close,
                   AVG(close) OVER (PARTITION BY code ORDER BY date
                       ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
                   AVG(close) OVER (PARTITION BY code ORDER BY date
                       ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS ma60,
                   close / LAG(close,1) OVER (PARTITION BY code ORDER BY date) - 1 AS pct_chg
            FROM read_parquet({fl})
            WHERE date <= DATE '{as_of}'
        ),
        computed AS (
            SELECT *,
                   COUNT(*) OVER (PARTITION BY code) AS hist_len,
                   SUM(CASE WHEN pct_chg >= {limit_expr} THEN 1 ELSE 0 END)
                       OVER (PARTITION BY code ORDER BY date
                             ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) AS limit_up_10d,
                   ABS(close - ma20) / NULLIF(ma20, 0) AS deviation
            FROM daily
        ),
        latest AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
            FROM computed
        )
        SELECT code, date, close, ma20, ma60, deviation, limit_up_10d
        FROM latest WHERE rn = 1
          AND hist_len >= {c['min_history']}
          AND close > ma60
          AND limit_up_10d >= {c['limit_up_10d']}
          AND deviation < {c['deviation_ma20_max']}
        ORDER BY deviation
        LIMIT {int(top_n)}
        """
        rows = con.execute(sql).fetchall()
        cols = [d[0] for d in con.description]
        hits = []
        for r in rows:
            d = dict(zip(cols, r))
            hits.append({
                "code": d["code"],
                "name": "",
                "price": round(float(d["close"]), 2),
                "ma20": round(float(d["ma20"]), 2),
                "ma60": round(float(d["ma60"]), 2),
                "deviation_pct": round(float(d["deviation"]) * 100, 2),
                "limit_up_10d": int(d["limit_up_10d"]),
                "date": str(d["date"])[:10],
            })
        _attach_names(hits)
        return hits
    finally:
        con.close()


def _attach_names(hits: list[dict]) -> None:
    """从 meta.db 批量补充股票中文名（就地修改 hits）。"""
    if not hits:
        return
    w = Warehouse()
    conn = w._conn()
    try:
        codes = [h["code"] for h in hits]
        placeholders = ",".join("?" * len(codes))
        rows = conn.execute(
            f"SELECT code, name FROM instruments WHERE code IN ({placeholders})",
            codes,
        ).fetchall()
        name_map = {r[0]: r[1] for r in rows}
        for h in hits:
            h["name"] = name_map.get(h["code"], "") or h["code"]
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════
# 图表数据：返回 ECharts 友好的多指标序列（指标可勾选）
# ═══════════════════════════════════════════════════════

# 可用指标及其中文名（前端勾选器用）
METRICS = {
    "close": "收盘价",
    "ma5": "ma5",
    "ma10": "ma10",
    "ma20": "ma20",
    "ma60": "ma60",
    "ma120": "ma120",
    "vol_ratio": "量比",
    "turn": "换手率",
    "amount": "成交额(亿)",
    "pe": "PE",
    "pb_mrq": "PB(MRQ)",
}


def get_series(code: str, as_of: str = "", days: int = 120,
               metrics: Optional[list[str]] = None) -> dict:
    """取单只股票的多指标时间序列（ECharts 数据）。

    Returns:
        {code, dates: [...], series: [{name, data: [...]}], metrics: {可选指标名: 中文}}
    """
    if metrics is None:
        metrics = ["close", "ma20", "ma60"]
    w = Warehouse()
    fl = _parquet_files(w)
    if not as_of:
        df = w.read_daily(w.available_months("daily")[-1])
        as_of = str(df["date"].max())[:10]

    # 动态拼窗口 SQL
    ma_cols = {m: f"AVG(close) OVER (ORDER BY date ROWS BETWEEN {w}-1 PRECEDING AND CURRENT ROW) AS {m}"
               for m, w in [("ma5",5),("ma10",10),("ma20",20),("ma60",60),("ma120",120)]}
    select_ma = ", ".join(ma_cols[m] for m in metrics if m in ma_cols)

    con = duckdb.connect()
    try:
        extra_sql = ""
        if select_ma:
            extra_sql = ", " + select_ma
        sql = f"""
        SELECT date, close, volume, amount, turn, peTTM, pbMRQ
               {extra_sql},
               volume / NULLIF(AVG(volume) OVER (ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),0) AS vol_ratio
        FROM read_parquet({fl})
        WHERE code = '{code}' AND date <= DATE '{as_of}'
        ORDER BY date DESC LIMIT {int(days)}
        """
        df = con.execute(sql).fetchdf().sort_values("date")
    finally:
        con.close()

    if df.empty:
        return {"code": code, "dates": [], "series": [], "metrics": METRICS}

    dates = [str(d)[:10] for d in df["date"]]
    series = []
    for m in metrics:
        if m not in df.columns:
            continue
        col = df[m]
        vals = []
        for v in col:
            if v is None or (isinstance(v, float) and (v != v)):  # NaN
                vals.append(None)
            elif m == "amount":
                vals.append(round(float(v) / 1e8, 2))  # 元→亿
            else:
                vals.append(round(float(v), 2))
        series.append({"name": METRICS.get(m, m), "key": m, "data": vals})

    # 策略买入信号点：历史上符合「股价>MA60 + 10日涨停≥1 + 偏离MA20<X%」的日期
    # 用 markPoint 标注（不画水平虚线，避免与指标实线重叠困惑）
    signals = _signal_points(code, as_of, days)
    return {"code": code, "dates": dates, "series": series, "signals": signals,
            "metrics": METRICS}


def _signal_points(code: str, as_of: str, days: int) -> list[dict]:
    """计算单只股票历史上触发策略条件的信号日期（返回 ECharts markPoint 数据）。"""
    from StockInvestmentTool.strategy_lab import _parquet_files
    w = Warehouse()
    fl = _parquet_files(w)
    con = duckdb.connect()
    try:
        limit_expr = _limit_pct_sql(0.098)
        sql = f"""
        WITH daily AS (
            SELECT code, date, close,
                   close / LAG(close,1) OVER (ORDER BY date) - 1 AS pct_chg,
                   AVG(close) OVER (ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
                   AVG(close) OVER (ORDER BY date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS ma60
            FROM read_parquet({fl})
            WHERE code = '{code}' AND date <= DATE '{as_of}'
        ),
        computed AS (
            SELECT *,
                   SUM(CASE WHEN pct_chg >= {limit_expr} THEN 1 ELSE 0 END)
                       OVER (ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) AS limit_up_10d,
                   ABS(close - ma20) / NULLIF(ma20, 0) AS deviation
            FROM daily
        )
        SELECT date, close FROM computed
        WHERE close > ma60 AND limit_up_10d >= 1 AND deviation < 0.10
        ORDER BY date DESC LIMIT {int(days)}
        """
        rows = con.execute(sql).fetchall()
        return [{"date": str(r[0])[:10], "value": round(float(r[1]), 2)} for r in rows]
    finally:
        con.close()


# ═══════════════════════════════════════════════════════
# 回测：历史选股 → 持有 N 天收益（vs 全市场基准）
# ═══════════════════════════════════════════════════════

def backtest(conditions: Optional[dict] = None, hold_days: int = 10,
             start: str = "2024-01-01", end: str = "") -> dict:
    """历史回测策略，返回收益统计 + 全市场基准对比。"""
    c = {**DEFAULT_CONDITIONS, **(conditions or {})}
    w = Warehouse()
    fl = _parquet_files(w)
    if not end:
        df = w.read_daily(w.available_months("daily")[-1])
        end = str(df["date"].max())[:10]

    con = duckdb.connect()
    try:
        limit_expr = _limit_pct_sql(c["limit_up_pct"])
        n = hold_days
        sql = f"""
        WITH daily AS (
            SELECT code, date, close,
                   AVG(close) OVER (PARTITION BY code ORDER BY date
                       ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
                   AVG(close) OVER (PARTITION BY code ORDER BY date
                       ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS ma60,
                   close / LAG(close,1) OVER (PARTITION BY code ORDER BY date) - 1 AS pct_chg
            FROM read_parquet({fl})
            WHERE date BETWEEN DATE '{start}' AND DATE '{end}'
        ),
        computed AS (
            SELECT *,
                   COUNT(*) OVER (PARTITION BY code) AS hist_len,
                   SUM(CASE WHEN pct_chg >= {limit_expr} THEN 1 ELSE 0 END)
                       OVER (PARTITION BY code ORDER BY date
                             ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) AS limit_up_10d,
                   ABS(close - ma20) / NULLIF(ma20, 0) AS deviation,
                   LEAD(close, {n}) OVER (PARTITION BY code ORDER BY date) AS close_n
            FROM daily
        )
        SELECT (close_n - close) / close AS ret
        FROM computed
        WHERE hist_len >= {c['min_history']}
          AND close > ma60
          AND limit_up_10d >= {c['limit_up_10d']}
          AND deviation < {c['deviation_ma20_max']}
          AND close_n IS NOT NULL
        """
        df = con.execute(sql).fetchdf()
        rets = df["ret"].dropna()
        # 全市场基准
        base = _market_baseline(fl, start, end, n)
    finally:
        con.close()

    if rets.empty:
        return {"signals": 0}

    avg_ret_pct = float(rets.mean()) * 100
    return {
        "signals": int(len(rets)),
        "avg_ret": round(avg_ret_pct, 2),
        "median_ret": round(float(rets.median()) * 100, 2),
        "win_rate": round(float((rets > 0).mean()) * 100, 1),
        "p25": round(float(rets.quantile(0.25)) * 100, 2),
        "p75": round(float(rets.quantile(0.75)) * 100, 2),
        "worst": round(float(rets.min()) * 100, 2),
        "best": round(float(rets.max()) * 100, 2),
        "hold_days": n,
        "market_avg_ret": base.get("avg_ret"),
        "market_win_rate": base.get("win_rate"),
        "excess_avg": (round(avg_ret_pct - base["avg_ret"], 2)
                       if base.get("avg_ret") is not None else None),
    }


def _market_baseline(fl: str, start: str, end: str, n: int) -> dict:
    """全市场基准：区间内所有股票任意时点持有 N 天的平均收益。"""
    con = duckdb.connect()
    try:
        sql = f"""
        WITH daily AS (
            SELECT code, date, close,
                   LEAD(close, {n}) OVER (PARTITION BY code ORDER BY date) AS close_n
            FROM read_parquet({fl})
            WHERE date BETWEEN DATE '{start}' AND DATE '{end}'
        )
        SELECT (close_n - close) / close AS ret
        FROM daily WHERE close_n IS NOT NULL
        """
        df = con.execute(sql).fetchdf()
        rets = df["ret"].dropna()
        return {"avg_ret": round(float(rets.mean()) * 100, 2),
                "win_rate": round(float((rets > 0).mean()) * 100, 1)}
    finally:
        con.close()


def backtest_curve(conditions: Optional[dict] = None, hold_days: int = 10,
                   start: str = "2024-01-01", end: str = "") -> dict:
    """策略 vs 大盘 的累计收益曲线（ECharts 对比图数据）。

    Returns:
        {dates, strategy: [...], market: [...], signals: 总信号数}
        strategy: 每个交易日的信号「平均持有N天收益」累计曲线
        market:   同日全市场等权「持有N天收益」累计曲线（大盘基准）
    """
    c = {**DEFAULT_CONDITIONS, **(conditions or {})}
    w = Warehouse()
    fl = _parquet_files(w)
    if not end:
        df = w.read_daily(w.available_months("daily")[-1])
        end = str(df["date"].max())[:10]

    con = duckdb.connect()
    try:
        limit_expr = _limit_pct_sql(c["limit_up_pct"])
        n = hold_days
        # 策略信号：每日触发的信号及持有N天收益（带信号日期）
        sql_sig = f"""
        WITH daily AS (
            SELECT code, date, close,
                   AVG(close) OVER (PARTITION BY code ORDER BY date
                       ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
                   AVG(close) OVER (PARTITION BY code ORDER BY date
                       ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS ma60,
                   close / LAG(close,1) OVER (PARTITION BY code ORDER BY date) - 1 AS pct_chg
            FROM read_parquet({fl})
            WHERE date BETWEEN DATE '{start}' AND DATE '{end}'
        ),
        computed AS (
            SELECT *,
                   COUNT(*) OVER (PARTITION BY code) AS hist_len,
                   SUM(CASE WHEN pct_chg >= {limit_expr} THEN 1 ELSE 0 END)
                       OVER (PARTITION BY code ORDER BY date
                             ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) AS limit_up_10d,
                   ABS(close - ma20) / NULLIF(ma20, 0) AS deviation,
                   LEAD(close, {n}) OVER (PARTITION BY code ORDER BY date) AS close_n
            FROM daily
        )
        SELECT date AS sig_date, (close_n - close) / close AS ret
        FROM computed
        WHERE hist_len >= {c['min_history']}
          AND close > ma60 AND limit_up_10d >= {c['limit_up_10d']}
          AND deviation < {c['deviation_ma20_max']} AND close_n IS NOT NULL
        """
        sig = con.execute(sql_sig).fetchdf()
        if sig.empty:
            return {"dates": [], "strategy": [], "market": [], "signals": 0}
        # 按信号日聚合：每天触发信号的「平均持有N天收益」
        sig_grp = sig.groupby("sig_date")["ret"].mean().reset_index()
        sig_grp.columns = ["date", "avg_ret"]
        # 累计平均收益曲线（逐日累加，避免连乘爆炸）
        sig_grp = sig_grp.sort_values("date").reset_index(drop=True)
        sig_grp["cum"] = sig_grp["avg_ret"].cumsum()

        # 大盘基准：全市场每日「持有N天收益」平均，同样累计
        sql_mkt = f"""
        WITH daily AS (
            SELECT date, code, close,
                   LEAD(close, {n}) OVER (PARTITION BY code ORDER BY date) AS close_n
            FROM read_parquet({fl})
            WHERE date BETWEEN DATE '{start}' AND DATE '{end}'
        )
        SELECT date, AVG((close_n - close) / close) AS avg_ret
        FROM daily WHERE close_n IS NOT NULL
        GROUP BY date ORDER BY date
        """
        mkt = con.execute(sql_mkt).fetchdf()
        mkt = mkt.sort_values("date").reset_index(drop=True)
        mkt["cum"] = mkt["avg_ret"].cumsum()
    finally:
        con.close()

    # 对齐到共同日期轴（按策略信号日取大盘同日值）
    dates = [str(d)[:10] for d in sig_grp["date"]]
    mkt_map = {str(d)[:10]: v for d, v in zip(mkt["date"], mkt["cum"])}
    strategy_cum = [round(v * 100, 2) for v in sig_grp["cum"]]
    market_cum = [round((mkt_map.get(d, 0)) * 100, 2) for d in dates]
    return {"dates": dates, "strategy": strategy_cum, "market": market_cum,
            "signals": int(len(sig))}


# ═══════════════════════════════════════════════════════
# 图表：命中股票折线图 → Config.CHART_DIR，供 web 展示
# ═══════════════════════════════════════════════════════

def plot_hits(conditions: Optional[dict] = None, as_of: str = "",
              hits: Optional[list[dict]] = None, max_plot: int = 5) -> list[str]:
    """为命中的前 N 只股票画折线图，存到 CHART_DIR，返回文件路径列表。"""
    c = {**DEFAULT_CONDITIONS, **(conditions or {})}
    if hits is None:
        hits = scan(c, as_of=as_of, top_n=max_plot)
    if not as_of:
        w0 = Warehouse()
        df = w0.read_daily(w0.available_months("daily")[-1])
        as_of = str(df["date"].max())[:10]

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    import glob

    for f in glob.glob("/usr/share/fonts/**/*.tt[fc]", recursive=True):
        try:
            font_manager.fontManager.addfont(f)
        except Exception:
            pass
    fonts = font_manager.fontManager.get_font_names()
    plt.rcParams["font.family"] = fonts[-1] if fonts else "sans-serif"

    w = Warehouse()
    fl = _parquet_files(w)
    os.makedirs(Config.CHART_DIR, exist_ok=True)
    paths = []
    con = duckdb.connect()
    try:
        for h in hits[:max_plot]:
            code = h["code"]
            sql = f"""
            SELECT date, close,
                   AVG(close) OVER (ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
                   AVG(close) OVER (ORDER BY date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS ma60
            FROM read_parquet({fl})
            WHERE code = '{code}' AND date <= DATE '{as_of}'
            ORDER BY date DESC LIMIT 120
            """
            df = con.execute(sql).fetchdf().sort_values("date")
            if df.empty:
                continue
            fig, ax = plt.subplots(figsize=(12, 5))
            ax.plot(df["date"], df["close"], label="收盘", color="#1a73e8", linewidth=1.3)
            ax.plot(df["date"], df["ma20"], label="MA20", color="#e67e22", linewidth=0.9)
            ax.plot(df["date"], df["ma60"], label="MA60", color="#27ae60", linewidth=0.9)
            ax.axvline(df["date"].iloc[-1], color="red", linestyle="--", linewidth=0.8)
            ax.set_title(f"{code}  {h['price']} | 偏离MA20 {h['deviation_pct']}% | 10日涨停{h['limit_up_10d']}次")
            ax.legend(loc="best")
            ax.grid(alpha=0.3)
            plt.xticks(rotation=45)
            fig.tight_layout()
            p = Config.CHART_DIR / f"strategy_{code}.png"
            fig.savefig(p, dpi=100)
            plt.close(fig)
            paths.append(str(p))
    finally:
        con.close()
    return paths
