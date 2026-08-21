# -*- coding: utf-8 -*-
"""DuckDB 查询层 — 全市场因子扫描（按需读 parquet，绝不常驻内存）

DuckDB 是嵌入式列存引擎，按查询起停、用完即释放，天然适配 2C2G 环境。
所有全市场扫描都通过这里执行，避免用 pandas 把全量载入内存。

用法:
    from StockInvestmentTool.warehouse.scanner import MarketScanner
    s = MarketScanner()
    hits = s.scan(
        start='2026-07-01', end='2026-08-20',
        where='vol_ratio > 2 AND ret_5d > 5 AND bias_ratio < 10',
        order_by='vol_ratio DESC', limit=50,
    )
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class MarketScanner:
    """基于 DuckDB 的全市场因子扫描器。"""

    def __init__(self, warehouse=None):
        from StockInvestmentTool.warehouse.storage import Warehouse
        self.warehouse = warehouse or Warehouse()

    def _months_between(self, start: str, end: str) -> list[str]:
        months = []
        d = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")
        while d <= end_dt:
            ym = d.strftime("%Y-%m")
            if ym not in months:
                months.append(ym)
            y, m = d.year, d.month
            d = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
        return months

    def _available_intersection(self, start: str, end: str, kind: str = "factor") -> list[str]:
        """请求区间 ∩ 已落盘分区月份"""
        want = set(self._months_between(start, end))
        have = set(self.warehouse.available_months(kind))
        return sorted(want & have)

    def scan(self, start: str, end: str,
             where: str = "",
             order_by: str = "vol_ratio DESC",
             limit: int = 50,
             kind: str = "factor",
             columns: str = "*") -> list[dict]:
        """全市场扫描。

        Args:
            start/end: 扫描日期区间 YYYY-MM-DD
            where: SQL 过滤条件（基于因子列名）
            order_by: 排序
            limit: 返回条数
            kind: factor（因子宽表）或 daily（原始日线）
            columns: 选择列
        """
        try:
            import duckdb
        except ImportError:
            logger.error("缺少 duckdb（pip install duckdb），无法扫描")
            return []

        months = self._available_intersection(start, end, kind)
        if not months:
            logger.warning("扫描区间无可用分区: %s ~ %s", start, end)
            return []

        directory = "factors" if kind == "factor" else "daily"
        base = self.warehouse.base_dir / directory
        files = [str(base / f"{m}.parquet") for m in months]

        con = duckdb.connect()
        try:
            # 取每个标的在区间内「最后一天」的数据（latest-state 扫描）
            sql = f"""
                WITH latest AS (
                    SELECT *
                    FROM read_parquet({_q(files)})
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) = 1
                )
                SELECT {columns}
                FROM latest
                WHERE date BETWEEN ? AND ?
                {('AND (' + where + ')') if where else ''}
                ORDER BY {order_by}
                LIMIT {int(limit)}
            """
            rows = con.execute(sql, [start, end]).fetchall()
            cols = [d[0] for d in con.description]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            con.close()  # 立即释放，不常驻

    def distinct_dates(self, start: str, end: str, code: Optional[str] = None,
                       kind: str = "daily") -> list[str]:
        """区间内可用的交易日（用于检查覆盖完整性）。"""
        try:
            import duckdb
        except ImportError:
            return []
        months = self._available_intersection(start, end, kind)
        if not months:
            return []
        directory = "factors" if kind == "factor" else "daily"
        base = self.warehouse.base_dir / directory
        files = [str(base / f"{m}.parquet") for m in months]
        con = duckdb.connect()
        try:
            where_code = f"WHERE code = ?" if code else ""
            q = f"SELECT DISTINCT date FROM read_parquet({_q(files)}) {where_code} ORDER BY date"
            params = [code] if code else []
            return [str(r[0])[:10] for r in con.execute(q, params).fetchall()]
        finally:
            con.close()


def _q(files: list[str]) -> str:
    """生成 read_parquet 的路径列表字面量，如 ['a.parquet','b.parquet']"""
    return "[" + ",".join("'" + f.replace("'", "''") + "'" for f in files) + "]"
