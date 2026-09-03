"""数据源统一抽象（FR-1.4）

设计意图（HLD §3.4 / ADR-6）：
  - 抽 `DataSource` 接口，业务层只依赖接口，不散落「warehouse 优先、baostock 兜底」
    的 if-else；
  - **数据源只返回原始行情**（固定列），不含指标列 —— 指标计算统一在上层经
    IndicatorContext，避免仓库源返回原始列、在线源返回带指标列导致列不一致；
  - 扩展点 = 实现 `DataSource`，新增数据源只加实现，不碰业务层。

固定返回列（fetch_kline）：
    date / open / high / low / close / volume / amount / pe_ttm / pb_mrq / turn
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional, Protocol

import pandas as pd

logger = logging.getLogger(__name__)
_CODE_RE = re.compile(r"^(?:sh|sz|bj)\d{6}$")

# 数据源固定输出列（原始行情，统一标准命名 pe_ttm/pb_mrq）
KLINE_COLUMNS = [
    "date", "open", "high", "low", "close",
    "volume", "amount", "pe_ttm", "pb_mrq", "turn",
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
    def _available_daily_columns(files: list[str]) -> set[str]:
        """探测 daily 分区真实存在的标准字段（工作项 2：只查真实字段）。"""
        import duckdb
        con = duckdb.connect()
        try:
            file_list = "[" + ",".join("'" + f + "'" for f in files) + "]"
            try:
                cols = con.execute(f"DESCRIBE SELECT * FROM read_parquet({file_list})").fetchall()
            except Exception:
                cols = []
            return {str(row[0]) for row in cols}
        finally:
            con.close()

    # 标准 snake_case 字段；pe_ttm/pb_mrq 为可选估值字段；code 仅作过滤不返回
    REQUIRED_DAILY_COLUMNS = ("date", "code", "open", "high", "low", "close", "volume", "amount")
    OPTIONAL_DAILY_COLUMNS = ("turn", "pe_ttm", "pb_mrq")
    RETURN_DAILY_COLUMNS = ("date", "open", "high", "low", "close",
                            "volume", "amount", "pe_ttm", "pb_mrq", "turn")

    def _select_daily(self, files: list[str], code: str, start: Optional[str],
                      end: Optional[str], days: int | None = None) -> pd.DataFrame:
        """构建安全的日线查询：只查真实存在的列，可选字段缺失补空列。

        必填字段缺失抛出明确契约错误；可选字段缺失记录 Schema Mismatch。
        """
        available = self._available_daily_columns(files)
        missing_required = sorted(set(self.REQUIRED_DAILY_COLUMNS) - available)
        if missing_required:
            raise ValueError(
                f"daily 分区缺少必填字段 {missing_required}（Schema Mismatch），拒绝返回空表伪装成功")
        missing_optional = sorted(set(self.OPTIONAL_DAILY_COLUMNS) - available)
        if missing_optional:
            logger.warning("daily 分区可选字段缺失 %s（Schema Mismatch），查询时补空列", missing_optional)
        columns = [c for c in self.RETURN_DAILY_COLUMNS if c in available]
        file_list = "[" + ",".join("'" + f + "'" for f in files) + "]"
        where = f"code = '{code}'"
        if start:
            where += f" AND date >= DATE '{start}'"
        if end:
            where += f" AND date <= DATE '{end}'"
        order = "ORDER BY date DESC" if days else "ORDER BY date"
        limit = f"LIMIT {days}" if days else ""
        df = self._query(
            f"SELECT {', '.join(columns)} FROM read_parquet({file_list}) "
            f"WHERE {where} {order} {limit}"
        )
        if days:
            df = df.sort_values("date")
        # 补可选字段空列，保证契约列齐全
        for c in self.OPTIONAL_DAILY_COLUMNS:
            if c not in df.columns:
                df[c] = None
        return self._finalize(df)

    @staticmethod
    def _query(sql: str) -> pd.DataFrame:
        import duckdb
        con = duckdb.connect()
        try:
            return con.execute(sql).df()
        finally:
            con.close()

    @staticmethod
    def _validated_code(code: str) -> str:
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher

        normalized = StockDataFetcher.normalize_code(code).replace(".", "").lower()
        if not _CODE_RE.fullmatch(normalized):
            raise ValueError("股票代码格式无效")
        return normalized

    @staticmethod
    def _validated_day(value: Optional[str], default: str) -> str:
        candidate = (value or default)[:10]
        try:
            date.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError(f"日期格式无效: {candidate}") from exc
        return candidate

    # ── 契约实现 ────────────────────────────────────────

    def fetch_kline(self, code: str, start: Optional[str] = None,
                    end: Optional[str] = None) -> pd.DataFrame:
        from datetime import datetime, timedelta

        code_nodot = self._validated_code(code)
        end = self._validated_day(end, (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"))
        start = self._validated_day(start, (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"))
        if start > end:
            raise ValueError("开始日期不能晚于结束日期")

        df = self._load_published_daily(code_nodot, start, end)
        return self._finalize(df)

    def fetch_daily_series(self, code: str, days: int = 750) -> pd.DataFrame:
        """个股图表单查询：取最近 N 个交易日的原始日线（tail(days)）。"""
        code_nodot = self._validated_code(code)
        days = int(days)
        if days < 1 or days > 5000:
            raise ValueError("days 必须在 1 到 5000 之间")
        # 不传 end_date，让访问层加载全部已发布分区，再按交易日 tail；
        # 传入当前日期会把月份选择收窄到当月，图表只能显示一根最新 K 线。
        df = self._load_published_daily(code_nodot, None, None, days=days)
        return df.tail(days)

    def _load_published_daily(self, code: str, start: str, end: str,
                              days: int | None = None) -> pd.DataFrame:
        """从 Published Dataset 读取日线（统一访问层，含版本/质量/checksum 治理）。

        无治理版本时回退直读分区（legacy，quality_status=LEGACY 由上层标记），
        契约校验仍由 _select_daily 保证。
        """
        from StockInvestmentTool.warehouse.datasets import DatasetAccessError, load_dataset

        try:
            result = load_dataset(
                self._warehouse, "stock_daily",
                start_date=start, end_date=end, symbols=[code],
                required_quality="WARNING", allow_legacy=False,
            )
            df = result.data
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.drop(columns=["code"], errors="ignore")
            keep = [c for c in self.RETURN_DAILY_COLUMNS if c in df.columns]
            df = df[keep]
            for c in self.OPTIONAL_DAILY_COLUMNS:
                if c not in df.columns:
                    df[c] = None
            if days:
                return df.tail(days)
            return df
        except DatasetAccessError as exc:
            logger.info("WarehouseSource 无治理版本(%s)，回退直读分区: %s", code, exc)
            files = self._daily_files(start)
            if not files:
                return pd.DataFrame()
            return self._select_daily(files, code, start, end, days=days)

    def fetch_snapshot(self, code: str) -> dict:
        """从在线源退化为空快照；仓库源不持实时快照。"""
        return {}

    def fetch_fundamental_history(self, code: str) -> pd.DataFrame:
        """Read the symbol-partitioned fundamentals warehouse data."""
        normalized = self._validated_code(code)
        frame = self._warehouse.read_fundamentals(normalized)
        return frame if frame is not None else pd.DataFrame()

    def fetch_instrument(self, code: str) -> dict:
        return self._warehouse.get_instrument(self._validated_code(code)) or {}

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
                  "pe_ttm", "pb_mrq", "turn"):
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
        out = df.copy()
        # 在线源返回 baostock 原始列名，统一映射为标准命名
        rename = {"peTTM": "pe_ttm", "pbMRQ": "pb_mrq"}
        out = out.rename(columns={k: v for k, v in rename.items() if k in out.columns})
        cols = [c for c in KLINE_COLUMNS if c in out.columns]
        out = out[cols].copy() if cols else out.copy()
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
        out = df.copy()
        # 统一标准命名（兼容 baostock 原始列）
        rename = {"peTTM": "pe_ttm", "pbMRQ": "pb_mrq"}
        out = out.rename(columns={k: v for k, v in rename.items() if k in out.columns})
        # 无指标列：保证只含原始列（即使底层多带了也剔除）
        cols = [c for c in KLINE_COLUMNS if c in out.columns]
        return out[cols].copy() if cols else out


def get_default_datasource():
    """全局默认数据源（FallbackDataSource：warehouse 优先、baostock 兜底）。"""
    return FallbackDataSource([WarehouseSource(), OnlineSource()])
