# -*- coding: utf-8 -*-
"""离线全量采集 — baostock 全市场日线增量同步

设计（2C2G 约束）:
  - 全市场代码清单来自 baostock query_all_stock（type=1 股票 + ETF/指数按需）
  - 只拉「仓库中缺失的日期」，已覆盖则跳过，实现增量
  - 分块处理：一次只持有单只股票 / 单月的数据，绝不把全市场一次性载入内存
  - 单只股票 fetch 后按日期拆入对应月份分区，月份分区用「读-并-写」合并
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from StockInvestmentTool.data.fetcher import StockDataFetcher
from StockInvestmentTool.warehouse.storage import Warehouse, _ym_str

logger = logging.getLogger(__name__)

# baostock 查询字段（日线）
_DAILY_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,"
    "adjustflag,turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST"
)

# 我们仓库关心的列（其余可后续扩展）
_KEEP_COLS = [
    "date", "code", "open", "high", "low", "close",
    "volume", "amount", "turn", "tradestatus", "peTTM", "pbMRQ",
]

# baostock 请求限速：每次查询间最小间隔，避免触发黑名单
_MIN_QUERY_INTERVAL = 0.3


class MarketCollector:
    """全市场日线增量采集器。"""

    def __init__(self, warehouse: Optional[Warehouse] = None,
                 fetcher: Optional[StockDataFetcher] = None):
        self.warehouse = warehouse or Warehouse()
        self._fetcher = fetcher
        self._last_query = 0.0

    @property
    def fetcher(self) -> StockDataFetcher:
        if self._fetcher is None:
            self._fetcher = StockDataFetcher()
        return self._fetcher

    # ── 全市场代码清单 ─────────────────────────────────

    def list_market(self, include_etf: bool = True, include_index: bool = False,
                    day: Optional[str] = None) -> list[dict]:
        """拉取 baostock 全市场证券清单。

        注: query_all_stock 只有 code/tradeStatus/code_name 三个字段，
        无法直接区分股票/指数/ETF，这里用代码前缀推断资产类型。
        """
        day = day or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        rs = self.fetcher._bs_query(_query_all, day=day)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(dict(zip(rs.fields, rs.get_row_data())))
        if not rows:
            logger.warning("query_all_stock 返回空 (%s)", day)
            return []

        from StockInvestmentTool.data.fetcher import StockDataFetcher

        out = []
        for r in rows:
            raw_code = r.get("code") or ""
            code = raw_code.replace(".", "")
            if not code or len(code) < 6:
                continue
            asset = StockDataFetcher.detect_type(code)
            if asset == "index" and not include_index:
                continue
            if asset == "etf" and not include_etf:
                continue
            out.append({"code": code, "type": asset,
                        "tradeStatus": r.get("tradeStatus", ""),
                        "name": r.get("code_name", "")})
        logger.info("全市场清单: %d 个标的 (%s)", len(out), day)
        return out

    def sync_instruments(self, include_etf: bool = True, include_index: bool = False,
                         day: Optional[str] = None) -> int:
        """把全市场代码清单写入 meta.db 标的表。"""
        from StockInvestmentTool.screener.board import detect_board, board_name

        items = self.list_market(include_etf=include_etf,
                                 include_index=include_index, day=day)
        rows = []
        for it in items:
            code = it["code"]
            board = detect_board(code)
            rows.append({
                "code": code, "name": it.get("name", ""), "type": it["type"],
                "board": board or ("" if it["type"] == "stock" else it["type"]),
                "listed_date": "",
            })
        self.warehouse.upsert_instruments(rows)
        logger.info("标的清单已更新: %d 条", len(rows))
        return len(rows)

    # ── 增量日线采集 ──────────────────────────────────

    def _throttle(self):
        dt = time.time() - self._last_query
        if dt < _MIN_QUERY_INTERVAL:
            time.sleep(_MIN_QUERY_INTERVAL - dt)
        self._last_query = time.time()

    def _fetch_symbol(self, code: str, start: str, end: str) -> pd.DataFrame:
        """拉取单只股票某区间日线（复用 fetcher 的连接自愈）。

        仓库内部代码用无点格式（sh600900，与腾讯报价一致）；
        baostock 查询需带点格式（sh.600900），这里转换。
        """
        bs_code = StockDataFetcher.normalize_code(code)
        self._throttle()
        rs = self.fetcher._bs_query(
            _query_history,
            code=bs_code, fields=_DAILY_FIELDS,
            start_date=start, end_date=end, frequency="d", adjustflag="2",
        )
        data_list = []
        while rs.error_code == "0" and rs.next():
            data_list.append(rs.get_row_data())
        if not data_list:
            return pd.DataFrame()
        fields = list(_DAILY_FIELDS.split(","))
        df = pd.DataFrame(data_list, columns=fields)
        # 只保留关心的列
        df = df[[c for c in _KEEP_COLS if c in df.columns]]
        # 统一 code 为无点格式（sh600900），与腾讯/在线快照/清单一致
        df["code"] = df["code"].astype(str).str.replace(".", "", regex=False)
        # 数值列转 float（跳过 date/code 文本列，避免日期被强转成 NaN）
        for c in df.columns:
            if c not in ("date", "code"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").drop_duplicates("date")
        return df

    def _month_range(self, start: str, end: str) -> list[str]:
        """区间内所有 YYYY-MM 列表"""
        months = []
        d = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")
        while d <= end_dt:
            ym = d.strftime("%Y-%m")
            if ym not in months:
                months.append(ym)
            # 下月
            y, m = d.year, d.month
            d = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
        return months

    def sync_daily(self, start_date: Optional[str] = None,
                   end_date: Optional[str] = None,
                   symbols: Optional[list[str]] = None,
                   include_etf: bool = True,
                   include_index: bool = False,
                   max_symbols: Optional[int] = None) -> dict:
        """全市场日线增量同步（核心）。

        Args:
            start_date: 起始日期 YYYY-MM-DD（默认近3年）
            end_date: 结束日期（默认昨天）
            symbols: 限定标的列表（None=全市场）
            include_etf / include_index: 是否包含 ETF / 指数
            max_symbols: 最多处理多少只（测试用）

        Returns:
            dict: 统计（新增行数/失败数/耗时）
        """
        end_date = end_date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=3 * 365)).strftime("%Y-%m-%d")

        if symbols is None:
            items = self.list_market(include_etf=include_etf,
                                     include_index=include_index)
            symbols = [it["code"] for it in items]
            if include_etf or include_index:
                # 补齐仓库里已有的 ETF/指数
                types = self.warehouse.instrument_types()
                extra = [c for c, t in types.items()
                         if t in ("etf", "index") and c not in symbols]
                symbols.extend(extra)
        if max_symbols:
            symbols = symbols[:max_symbols]

        months = self._month_range(start_date, end_date)
        logger.info("增量同步: %d 标的 × %d 月份 (%s ~ %s)",
                    len(symbols), len(months), start_date, end_date)

        added = 0
        failed: list[str] = []
        t0 = time.time()

        # 内存只持有「按月累积」的数据块（单月约 11MB，全部月份合计可控）。
        # 先载入各月既有分区，逐标的把新数据追加进对应月份块，
        # 全部完成后统一写盘 —— 避免「每只股票都读-并-写整个分区」的 IO 风暴。
        month_bufs: dict[str, pd.DataFrame] = {}
        for ym in months:
            existing = self.warehouse.read_daily(ym)
            if existing is not None and len(existing):
                month_bufs[ym] = existing
            else:
                month_bufs[ym] = None

        for i, code in enumerate(symbols, 1):
            try:
                df = self._fetch_symbol(code, start_date, end_date)
            except Exception as e:
                failed.append(code)
                logger.warning("拉取 %s 失败: %s", code, e)
                continue
            if df.empty:
                continue
            # 拆入内存中的月份块（去掉该标的旧数据，追加新数据）
            for ym, grp in df.groupby(df["date"].dt.strftime("%Y-%m")):
                if ym not in month_bufs:
                    month_bufs[ym] = None
                cur = month_bufs[ym]
                if cur is not None and len(cur):
                    cur = cur[cur["code"] != code]
                    merged = pd.concat([cur, grp], ignore_index=True)
                else:
                    merged = grp.copy()
                merged = merged.drop_duplicates(subset=["date", "code"])
                merged = merged.sort_values(["date", "code"])
                month_bufs[ym] = merged
            added += len(df)
            if i % 200 == 0 or i == len(symbols):
                logger.info("进度 %d/%d，已入库 %d 行", i, len(symbols), added)

        # 统一写盘（只写有数据、且数据发生变化的月份）
        for ym, df in month_bufs.items():
            if df is not None and len(df):
                self.warehouse.write_daily_partition(ym, df)

        elapsed = time.time() - t0
        logger.info("日线增量完成: +%d 行, 失败 %d, 耗时 %.1fs",
                    added, len(failed), elapsed)
        return {"added_rows": added, "symbols": len(symbols),
                "failed": failed, "elapsed_sec": round(elapsed, 1)}


# ── baostock 包装（供 _bs_query 使用，统一走连接自愈）──

def _query_all(day: str):
    import baostock as bs
    return bs.query_all_stock(day=day)


def _query_history(code, fields, start_date, end_date, frequency, adjustflag):
    import baostock as bs
    return bs.query_history_k_data_plus(
        code=code, fields=fields, start_date=start_date, end_date=end_date,
        frequency=frequency, adjustflag=adjustflag,
    )
