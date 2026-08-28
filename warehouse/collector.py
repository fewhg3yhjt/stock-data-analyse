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
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from StockInvestmentTool.datasource.fetcher import StockDataFetcher
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
                 fetcher: Optional[StockDataFetcher] = None,
                 query_interval: Optional[float] = None):
        self.warehouse = warehouse or Warehouse()
        self._fetcher = fetcher
        self._last_query = 0.0
        # 查询节流间隔（秒）：大流量采集时调大更安全，防 baostock 限流/封 IP
        self.query_interval = query_interval if query_interval is not None \
            else float(os.getenv("BAOSTOCK_QUERY_INTERVAL", str(_MIN_QUERY_INTERVAL)))

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

        from StockInvestmentTool.datasource.fetcher import StockDataFetcher

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
        if dt < self.query_interval:
            time.sleep(self.query_interval - dt)
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

    # ── 腾讯历史K线数据源（备胎/主源，不封IP）──────────────

    def _fetch_symbol_tencent(self, code: str, start: str, end: str) -> pd.DataFrame:
        """用腾讯 newfqkline 接口拉取单只标的日线（前复权，含成交额/换手率）。

        接口: proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get
        param: <code>,day,<start>,<end>,<count>,qfq
        返回 [date, open, close, high, low, volume(手), {}, turn%, amount(万), ...]
        快、稳定、不封 IP；count 上限约 800 根（3 年+）。
        """
        import requests

        self._throttle()
        code_tencent = code  # sh600900 无点，与腾讯一致
        url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
        # 计算需要多少根（按交易日 ~244/年）
        need_days = (datetime.strptime(end, "%Y-%m-%d")
                     - datetime.strptime(start, "%Y-%m-%d")).days
        count = max(need_days, 800)

        param = f"{code_tencent},day,{start},{end},{count},qfq"
        try:
            resp = requests.get(url, params={"param": param}, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            raise ConnectionError(f"腾讯K线拉取失败 {code}: {e}") from e

        data = resp.json().get("data", {}).get(code_tencent, {})
        if isinstance(data, list):  # 偶发返回 list 结构
            rows = data
        else:
            rows = data.get("qfqday") or data.get("day") or []

        if not rows:
            return pd.DataFrame()

        # 行格式: [date, open, close, high, low, volume(手), {}, turn%, amount(万), ...]
        records = []
        for r in rows:
            if len(r) < 6:
                continue
            rec = {
                "date": r[0],
                "open": r[1], "close": r[2],
                "high": r[3], "low": r[4],
                "volume": r[5],  # 单位「手」，稍后 ×100 转股
            }
            if len(r) >= 9:  # proxy 接口含换手率/成交额
                rec["turn"] = r[7]           # 换手率 %
                rec["amount"] = r[8]         # 成交额（万元）→ 稍后转元
            records.append(rec)
        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)
        df["code"] = code
        for c in ("open", "high", "low", "close"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        # 腾讯 volume 单位是「手」，统一转成「股」（×100），与 baostock 对齐
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100
        # 成交额: 万元 → 元（×10000），与 baostock 对齐
        if "amount" in df.columns:
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce") * 10000
        if "turn" in df.columns:
            df["turn"] = pd.to_numeric(df["turn"], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        # 过滤到请求区间内
        df = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
        df = df.sort_values("date").drop_duplicates("date")
        # 对齐 _KEEP_COLS（腾讯无 pe/pb/tradestatus，用 NaN 占位）
        for c in ("tradestatus", "peTTM", "pbMRQ"):
            df[c] = float("nan")
        df = df[[c for c in _KEEP_COLS if c in df.columns]]
        return df

    def sync_daily(self, start_date: Optional[str] = None,
                   end_date: Optional[str] = None,
                   symbols: Optional[list[str]] = None,
                   include_etf: bool = True,
                   include_index: bool = False,
                   max_symbols: Optional[int] = None,
                   flush_every: int = 1000,
                   source: str = "baostock",
                   target: str = "daily",
                   progress_callback=None, job_run_id: Optional[int] = None,
                   capture_raw: Optional[bool] = None,
                   asset_types: Optional[list[str]] = None) -> dict:
        """全市场日线增量同步（核心）。

        Args:
            start_date: 起始日期 YYYY-MM-DD（默认近3年）
            end_date: 结束日期（默认昨天）
            symbols: 限定标的列表（None=全市场）
            include_etf / include_index: 是否包含 ETF / 指数
            max_symbols: 最多处理多少只（测试用）
            flush_every: 每处理 N 个标的就落盘一次，控制内存峰值（2C2G 安全）
            source: 数据源 baostock（默认）/ tencent（腾讯，不封IP）
            target: 写入目标
                daily     → 加工层 daily/ 分区（旧行为，直接写完整宽表）
                raw:<src> → 贴源层 raw/<src>/ 分区（源数据独立存放，不覆盖）

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
        from StockInvestmentTool.warehouse.asset_profiles import select_symbols
        symbols, asset_type_counts = select_symbols(
            symbols, asset_types=asset_types, known_types=self.warehouse.instrument_types()
        )
        if max_symbols:
            symbols = symbols[:max_symbols]

        capture_raw = (source == "tencent" and target == "daily") if capture_raw is None else capture_raw
        batch_store = None
        batch_id = None
        raw_writer = None
        raw_capture_failed = False
        if capture_raw:
            from StockInvestmentTool.warehouse.source_batches import SourceBatchStore
            run_date = datetime.now().strftime("%Y-%m-%d")
            universe_id = f"stock_etf_active_{run_date.replace('-', '')}"
            batch_store = SourceBatchStore(self.warehouse.meta_db_path)
            batch_id = batch_store.start(
                run_date=run_date, trade_date_start=start_date, trade_date_end=end_date,
                expected_symbols=len(symbols), universe_id=universe_id,
                request_context={"source": source, "symbols_limited": max_symbols is not None,
                                  "include_etf": include_etf, "include_index": include_index},
                job_run_id=job_run_id, source_name=source,
            )
            raw_writer = self.warehouse.raw.begin_batch(
                source, "stock_daily", datetime.now().strftime("%Y-%m-%d")
            )

        months = self._month_range(start_date, end_date)
        logger.info("增量同步: %d 标的 × %d 月份 (%s ~ %s)",
                    len(symbols), len(months), start_date, end_date)

        added = 0
        failed: list[str] = []
        skipped = 0
        t0 = time.time()

        # 解析写入目标
        raw_source = None
        if target.startswith("raw:"):
            raw_source = target.split(":", 1)[1]

        # 构建「每个标的自有最后日期」索引 {code: last_date}
        # 用 DuckDB 对全部分区 GROUP BY code 取 max(date)（秒级，不占常驻内存）。
        # 增量判断依据是"该标的自有最后日期"，而非"仓库全局最新交易日"：
        #   - 某天没拉到 → 下次检测到 last_date < end_date → 自动补拉缺失区间
        #   - 已到 end_date → 跳过（不重拉）
        last_dates: dict[str, pd.Timestamp] = {}
        if raw_source:
            lm_months = self.warehouse.raw.available_months(raw_source)
            months_to_scan = lm_months
        else:
            months_to_scan = self.warehouse.available_months("daily")
        if months_to_scan:
            try:
                import duckdb
                files = []
                for ym in months_to_scan:
                    if raw_source:
                        files.append(str(self.warehouse.raw.partition_path(raw_source, ym)))
                    else:
                        files.append(str(self.warehouse.daily_partition(ym)))
                files = [f for f in files if Path(f).exists()]
                if files:
                    con = duckdb.connect()
                    try:
                        file_list = "[" + ",".join("'" + f + "'" for f in files) + "]"
                        rows = con.execute(
                            f"SELECT code, MAX(date) AS last_date FROM read_parquet({file_list}) GROUP BY code"
                        ).fetchall()
                        last_dates = {r[0]: pd.Timestamp(r[1]) for r in rows}
                    finally:
                        con.close()
            except Exception as e:
                logger.warning("构建标的最新日期索引失败(%s)，退回旧覆盖判断", e)
        logger.info("已覆盖标的: %d 个（跳过）", len(last_dates))

        # 内存只持有「按月累积」的数据块；每 flush_every 个标的落盘一次并清空，
        # 避免全市场 × 多月在内存中累积过高（2C2G 下 OOM 风险）。
        month_bufs: dict[str, Optional[pd.DataFrame]] = {}

        def _flush():
            """把内存中的月份块合并写入磁盘，然后清空。"""
            for ym, df in month_bufs.items():
                if df is None or len(df) == 0:
                    continue
                if raw_source:
                    # 写贴源层：该源分区独立，仅追加/合并本标的
                    self.warehouse.raw.write(raw_source, ym, df)
                else:
                    # 写加工层 daily
                    existing = self.warehouse.read_daily(ym)
                    if existing is not None and len(existing):
                        df = pd.concat([existing, df], ignore_index=True)
                    df = df.drop_duplicates(subset=["date", "code"])
                    df = df.sort_values(["date", "code"])
                    self.warehouse.write_daily_partition(ym, df)
            month_bufs.clear()

        end_ts = pd.Timestamp(end_date)
        for i, code in enumerate(symbols, 1):
            if progress_callback:
                progress_callback(i - 1, len(symbols), code, "读取日线")
            # 增量判断：该标的自有最后日期 >= end_date → 已覆盖，跳过
            last = last_dates.get(code)
            if last is not None and last >= end_ts:
                skipped += 1
                if progress_callback:
                    progress_callback(i, len(symbols), code, "已是最新")
                continue

            # 只拉缺失区间：已有数据的拉 (last_date+1, end_date]，无数据拉全区间
            if last is not None:
                fetch_start = (last + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                fetch_start = start_date
            if fetch_start > end_date:
                skipped += 1
                if progress_callback:
                    progress_callback(i, len(symbols), code, "已是最新")
                continue

            try:
                if source == "tencent":
                    df = self._fetch_symbol_tencent(code, fetch_start, end_date)
                else:
                    df = self._fetch_symbol(code, fetch_start, end_date)
            except Exception as e:
                failed.append(code)
                logger.warning("拉取 %s 失败: %s", code, e)
                if progress_callback:
                    progress_callback(i, len(symbols), code, "拉取失败")
                continue
            if df.empty:
                skipped += 1
                if progress_callback:
                    progress_callback(i, len(symbols), code, "无新增数据")
                continue
            if capture_raw and not raw_capture_failed:
                try:
                    raw_frame = df.copy()
                    # Keep Tencent's transport units in Raw; YAML-driven Builder
                    # performs the single canonical conversion to shares/yuan.
                    if source == "tencent":
                        raw_frame["volume"] = raw_frame["volume"] / 100
                        raw_frame["amount"] = raw_frame["amount"] / 10000
                    raw_writer.append(raw_frame)
                except Exception:
                    raw_capture_failed = True
                    raw_writer.abort()
                    raw_writer = None
                    logger.exception("Raw Batch 采集过程中写入失败")
            # 拆入内存中的月份块（去掉该标的旧数据，追加新数据）
            for ym, grp in df.groupby(df["date"].dt.strftime("%Y-%m")):
                cur = month_bufs.get(ym)
                if cur is not None and len(cur):
                    cur = cur[cur["code"] != code]
                    merged = pd.concat([cur, grp], ignore_index=True)
                else:
                    merged = grp.copy()
                merged = merged.drop_duplicates(subset=["date", "code"])
                merged = merged.sort_values(["date", "code"])
                month_bufs[ym] = merged
            added += len(df)
            if progress_callback:
                progress_callback(i, len(symbols), code, "已入库")
            if i % flush_every == 0 or i == len(symbols):
                _flush()
                logger.info("进度 %d/%d，已入库 %d 行（已落盘）", i, len(symbols), added)

        # 兜底落盘（flush_every > 总标的时）
        if month_bufs:
            _flush()

        elapsed = time.time() - t0
        raw_result = None
        if capture_raw and batch_store and batch_id:
            try:
                if raw_writer is None:
                    raise ValueError("Raw Batch 写入失败或没有可写数据")
                raw_result = raw_writer.finish()
                batch_status = "partial_success" if failed else "success"
                batch_store.finish(batch_id, success_symbols=len(symbols) - len(failed) - skipped,
                                   failed_symbols=len(failed), skipped_symbols=skipped,
                                   row_count=raw_result["row_count"], raw_path=str(raw_result["path"]),
                                   checksum=raw_result["checksum"], file_size=raw_result["file_size"],
                                   status=batch_status, failure_details=failed)
            except Exception as exc:
                raw_capture_failed = True
                logger.error("Raw Batch 写入失败，不阻断旧 daily: %s", exc)
                batch_store.finish(batch_id, success_symbols=len(symbols) - len(failed) - skipped,
                                   failed_symbols=len(failed), skipped_symbols=skipped, row_count=0,
                                   raw_path=None, checksum=None, file_size=None, status="failed",
                                   error_summary=str(exc), failure_details=failed)
        logger.info("日线增量完成: +%d 行, 失败 %d, 耗时 %.1fs",
                    added, len(failed), elapsed)
        if raw_writer is not None:
            raw_writer.abort()
        return {"added_rows": added, "symbols": len(symbols),
                "failed": failed, "up_to_date": not failed and added == 0,
                "rows": added, "elapsed_sec": round(elapsed, 1),
                "source_batch_id": batch_id, "source_batch_ids": [batch_id] if batch_id else [],
                "raw_capture_failed": raw_capture_failed,
                "skipped_symbols": skipped,
                "asset_type_counts": asset_type_counts,
                "raw_batch": raw_result}


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
