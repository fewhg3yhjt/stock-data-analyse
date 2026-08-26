"""数据源统一抽象（FR-1.4）

设计意图（HLD §3.4 / ADR-6）：
  - 抽 `DataSource` 接口，业务层只依赖接口，不散落「warehouse 优先、baostock 兜底」
    的 if-else；
  - **数据源只返回原始行情**（固定列），不含指标列 —— 指标计算统一在上层经
    IndicatorContext，避免仓库源返回原始列、在线源返回带指标列导致列不一致；
  - 扩展点 = 实现 `DataSource`，新增数据源只加实现，不碰业务层。

固定返回列（fetch_kline）：
    date / open / high / low / close / volume / amount / peTTM / pbMRQ / turn
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Protocol

import pandas as pd

logger = logging.getLogger(__name__)

# 数据源固定输出列（原始行情）
KLINE_COLUMNS = [
    "date", "open", "high", "low", "close",
    "volume", "amount", "peTTM", "pbMRQ", "turn",
]


class DataSource(Protocol):
    """统一数据源契约：只返回原始行情，不含指标列。"""

    def fetch_kline(self, code: str, start: Optional[str] = None,
                    end: Optional[str] = None) -> pd.DataFrame:
        """按代码 + 日期区间取原始 K 线（列固定 KLINE_COLUMNS）。"""
        ...

    def fetch_daily_series(self, code: str, days: int = 750) -> pd.DataFrame:
        """个股图表专用：取最近 N 个交易日的原始日线。"""
        ...

    def fetch_snapshot(self, code: str) -> dict:
        """取实时快照（现价/涨跌/量/换手/量比/PE/PB）。"""
        ...

    def fetch_minute_series(self, code: str, day: Optional[str] = None) -> pd.DataFrame:
        """取指定交易日分钟序列。"""
        ...


class WarehouseSource:
    """离线仓库源：DuckDB 跨分区单查询。

    只读 warehouse/daily 分区（Parquet 月分区），一次 DuckDB 查询按 code + 区间过滤，
    避免逐月 `read_parquet` + `df[df.code==...]` 的内存低效（P1）。
    """

    def __init__(self, warehouse=None):
        from StockInvestmentTool.warehouse.storage import Warehouse
        self._warehouse = warehouse or Warehouse()

    # ── 内部 ────────────────────────────────────────────

    def _daily_files(self, start: Optional[str]) -> list[str]:
        w = self._warehouse
        months = w.available_months("daily")
        files = [str(w.daily_partition(ym)) for ym in months]
        return [f for f in files if Path(f).exists()]

    @staticmethod
    def _query(sql: str) -> pd.DataFrame:
        import duckdb
        con = duckdb.connect()
        try:
            return con.execute(sql).df()
        finally:
            con.close()

    # ── 契约实现 ────────────────────────────────────────

    def fetch_kline(self, code: str, start: Optional[str] = None,
                    end: Optional[str] = None) -> pd.DataFrame:
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher
        from datetime import datetime, timedelta

        code_nodot = StockDataFetcher.normalize_code(code).replace(".", "")
        if end is None:
            end = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if start is None:
            start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        files = self._daily_files(start)
        if not files:
            return pd.DataFrame()
        try:
            file_list = "[" + ",".join("'" + f + "'" for f in files) + "]"
            df = self._query(
                f"""SELECT date, open, high, low, close, volume, amount, turn, peTTM, pbMRQ
                    FROM read_parquet({file_list})
                    WHERE code = '{code_nodot}'
                      AND date >= DATE '{start}' AND date <= DATE '{end}'
                    ORDER BY date"""
            )
            return self._finalize(df)
        except Exception as e:
            logger.warning("WarehouseSource.fetch_kline %s 失败(%s)", code_nodot, e)
            return pd.DataFrame()

    def fetch_daily_series(self, code: str, days: int = 750) -> pd.DataFrame:
        """个股图表单查询：取最近 N 个交易日的原始日线（tail(days)）。"""
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher

        code_nodot = StockDataFetcher.normalize_code(code).replace(".", "")
        files = self._daily_files(None)
        if not files:
            return pd.DataFrame()
        try:
            file_list = "[" + ",".join("'" + f + "'" for f in files) + "]"
            df = self._query(
                f"""SELECT date, open, high, low, close, volume, amount, turn, peTTM, pbMRQ
                    FROM read_parquet({file_list})
                    WHERE code = '{code_nodot}'
                    ORDER BY date DESC
                    LIMIT {int(days)}"""
            )
            df = df.sort_values("date")
            return self._finalize(df)
        except Exception as e:
            logger.warning("WarehouseSource.fetch_daily_series %s 失败(%s)", code_nodot, e)
            return pd.DataFrame()

    def fetch_snapshot(self, code: str) -> dict:
        """从在线源退化为空快照；仓库源不持实时快照。"""
        return {}

    def fetch_minute_series(self, code: str, day: Optional[str] = None) -> pd.DataFrame:
        from datetime import datetime

        target = day or datetime.now().strftime("%Y-%m-%d")
        return self._warehouse.minute_store().read(target, code)

    @staticmethod
    def _finalize(df: pd.DataFrame) -> pd.DataFrame:
        import pandas as pd
        if df is None or df.empty:
            return pd.DataFrame()
        for c in ("open", "high", "low", "close", "volume", "amount",
                  "peTTM", "pbMRQ", "turn"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        return df.drop_duplicates("date").sort_values("date").reset_index(drop=True)


class OnlineSource:
    """在线源：baostock / AkShare / 腾讯，带仓库兜底。

    当 warehouse 无数据时回退在线拉取（复用 StockDataFetcher 的连接自愈能力）。
    返回列对齐 KLINE_COLUMNS（不含指标列）。
    """

    def __init__(self):
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher
        self._fetcher = StockDataFetcher()

    def fetch_kline(self, code: str, start: Optional[str] = None,
                    end: Optional[str] = None) -> pd.DataFrame:
        raw = self._fetcher.get_kline(code, start, end)
        if raw is None or raw.empty:
            return pd.DataFrame()
        return self._select_columns(raw)

    def fetch_daily_series(self, code: str, days: int = 750) -> pd.DataFrame:
        from datetime import datetime, timedelta
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=int(days * 1.6))).strftime("%Y-%m-%d")
        raw = self._fetcher.get_kline(code, start, end)
        if raw is None or raw.empty:
            return pd.DataFrame()
        return self._select_columns(raw).tail(days)

    def fetch_snapshot(self, code: str) -> dict:
        # 实时快照不全在 baostock：交给调用方用 _realtime_enhance（腾讯/AkShare）
        return {}

    def fetch_minute_series(self, code: str, day: Optional[str] = None) -> pd.DataFrame:
        from StockInvestmentTool.warehouse.minute import fetch_tencent_minute

        frame = fetch_tencent_minute(code)
        if day and not frame.empty:
            frame = frame[frame["trade_date"] == str(day)[:10]]
        return frame

    @staticmethod
    def _select_columns(df: pd.DataFrame) -> pd.DataFrame:
        import pandas as pd
        if df is None or df.empty:
            return pd.DataFrame()
        cols = [c for c in KLINE_COLUMNS if c in df.columns]
        out = df[cols].copy() if cols else df.copy()
        out["date"] = pd.to_datetime(out["date"])
        return out.drop_duplicates("date").sort_values("date").reset_index(drop=True)


class FallbackDataSource:
    """聚合源：warehouse 优先，OnlineSource 兜底（封装原 monitor if-else）。

    业务层只依赖 `DataSource` 接口，新增数据源只需实现接口并在优先级链里注册。
    """

    def __init__(self, sources: Optional[list] = None):
        # 优先级从高到低；默认 [WarehouseSource, OnlineSource]
        self.sources = sources or [WarehouseSource(), OnlineSource()]

    def fetch_kline(self, code: str, start: Optional[str] = None,
                    end: Optional[str] = None) -> pd.DataFrame:
        for src in self.sources:
            try:
                df = src.fetch_kline(code, start, end)
            except Exception as e:
                logger.warning("数据源 %s 拉取失败(%s)，尝试下一源", type(src).__name__, e)
                continue
            if df is not None and not df.empty:
                return self._normalize(df)
        return pd.DataFrame()

    def fetch_daily_series(self, code: str, days: int = 750) -> pd.DataFrame:
        for src in self.sources:
            try:
                df = src.fetch_daily_series(code, days)
            except Exception as e:
                logger.warning("数据源 %s 序列拉取失败(%s)，尝试下一源", type(src).__name__, e)
                continue
            if df is not None and not df.empty:
                return self._normalize(df)
        return pd.DataFrame()

    def fetch_snapshot(self, code: str) -> dict:
        for src in self.sources:
            try:
                snap = src.fetch_snapshot(code)
            except Exception:
                continue
            if snap:
                return snap
        return {}

    def fetch_minute_series(self, code: str, day: Optional[str] = None) -> pd.DataFrame:
        for src in self.sources:
            try:
                frame = src.fetch_minute_series(code, day)
            except Exception as e:
                logger.warning("数据源 %s 分钟序列失败(%s)，尝试下一源", type(src).__name__, e)
                continue
            if frame is not None and not frame.empty:
                return frame
        return pd.DataFrame()

    @staticmethod
    def _normalize(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        # 无指标列：保证只含原始列（即使底层多带了也剔除）
        cols = [c for c in KLINE_COLUMNS if c in df.columns]
        return df[cols].copy() if cols else df


def get_default_datasource():
    """全局默认数据源（FallbackDataSource：warehouse 优先、baostock 兜底）。"""
    return FallbackDataSource([WarehouseSource(), OnlineSource()])
