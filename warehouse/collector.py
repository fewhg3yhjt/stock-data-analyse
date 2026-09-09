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
    "volume", "amount", "turn", "tradestatus", "pe_ttm", "pb_mrq",
]

# baostock 请求限速：每次查询间最小间隔，避免触发黑名单
_MIN_QUERY_INTERVAL = 0.3
_DEFAULT_TENCENT_TIMEOUT = 15.0


class CollectionTimeout(TimeoutError):
    """单次采集任务达到 deadline。"""


class MarketCollector:
    """全市场日线增量采集器。"""

    def __init__(self, warehouse: Optional[Warehouse] = None,
                 fetcher: Optional[StockDataFetcher] = None,
                 query_interval: Optional[float] = None,
                 tencent_timeout: Optional[float] = None):
        self.warehouse = warehouse or Warehouse()
        self._fetcher = fetcher
        self._last_query = 0.0
        # 查询节流间隔（秒）：大流量采集时调大更安全，防 baostock 限流/封 IP
        self.query_interval = query_interval if query_interval is not None \
            else float(os.getenv("BAOSTOCK_QUERY_INTERVAL", str(_MIN_QUERY_INTERVAL)))
        self.tencent_timeout = tencent_timeout if tencent_timeout is not None else float(
            os.getenv("TENCENT_HTTP_TIMEOUT", str(_DEFAULT_TENCENT_TIMEOUT)))

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
        """把全市场代码清单写入 management.db 标的目录。"""
        from StockInvestmentTool.screener.board import detect_board, board_name

        items = self.list_market(include_etf=include_etf,
                                 include_index=include_index, day=day)
        snapshot_date = day or datetime.now().strftime("%Y-%m-%d")
        rows = []
        for it in items:
            code = it["code"]
            board = detect_board(code)
            rows.append({
                "code": code, "name": it.get("name", ""), "type": it["type"],
                "board": board or ("" if it["type"] == "stock" else it["type"]),
                "listed_date": "",
                "trade_status": it.get("tradeStatus", ""),
                "universe_status": "active" if str(it.get("tradeStatus", "")).lower() in {"1", "active", "trading", "正常", "交易", ""} else "suspended",
                "first_seen_date": snapshot_date,
                "last_seen_date": snapshot_date,
                "last_source": "baostock",
            })
        self.warehouse.upsert_instruments(rows)
        from StockInvestmentTool.warehouse.universe import UniverseStore
        universe_store = UniverseStore(self.warehouse.meta_db_path)
        if items:
            universe_store.reconcile_authoritative_snapshot(snapshot_date, items, source="baostock")
        else:
            universe_store.record_snapshot(
                snapshot_date, items, source="baostock", authoritative=False, complete=False,
                error_message="全量 Universe 返回空",
                metadata={"include_etf": include_etf, "include_index": include_index},
            )
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

    def _fetch_symbol_tencent(self, code: str, start: str, end: str,
                              request_timeout: Optional[float] = None) -> pd.DataFrame:
        """用腾讯 newfqkline 接口拉取单只标的日线（前复权，含成交额/换手率）。

        接口: proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get
        param: <code>,day,<start>,<end>,<count>,qfq
        返回 [date, open, close, high, low, volume, {}, turn%, amount, ...]
         快、稳定、不封 IP；count 上限约 800 根（3 年+）。Raw 保留接口返回值，
         单位清洗由 DailyBuilder 负责。
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
            resp = requests.get(url, params={"param": param},
                                timeout=request_timeout if request_timeout is not None else self.tencent_timeout)
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

        # 行格式: [date, open, close, high, low, volume, {}, turn%, amount, ...]
        records = []
        for r in rows:
            if len(r) < 6:
                continue
            rec = {
                "date": r[0],
                "open": r[1], "close": r[2],
                "high": r[3], "low": r[4],
                "volume": r[5],  # 保留接口返回值，单位转换由 DailyBuilder 执行
            }
            if len(r) >= 9:  # proxy 接口含换手率/成交额
                rec["turn"] = r[7]           # 换手率 %
                rec["amount"] = r[8]         # 保留接口返回值，单位转换由 DailyBuilder 执行
            records.append(rec)
        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)
        df["code"] = code
        for c in ("open", "high", "low", "close"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        if "turn" in df.columns:
            df["turn"] = pd.to_numeric(df["turn"], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        # 过滤到请求区间内
        df = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
        df = df.sort_values("date").drop_duplicates("date")
        # 对齐 _KEEP_COLS（腾讯无 pe/pb/tradestatus，用 NaN 占位）
        for c in ("tradestatus", "pe_ttm", "pb_mrq"):
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
                    target: Optional[str] = None,
                   progress_callback=None, job_run_id: Optional[int] = None,
                   capture_raw: Optional[bool] = None,
                     asset_types: Optional[list[str]] = None,
                     force_refresh: bool = False,
                     timeout: Optional[float] = None,
                     deadline: Optional[float | datetime] = None,
                     request_context: Optional[dict] = None) -> dict:
        """全市场日线增量同步（核心）。

        Args:
            start_date: 起始日期 YYYY-MM-DD（必须显式传入）
            end_date: 结束日期 YYYY-MM-DD（必须显式传入）
            symbols: 限定标的列表（None=全市场）
            include_etf / include_index: 是否包含 ETF / 指数
            max_symbols: 最多处理多少只（测试用）
            flush_every: 每处理 N 个标的就落盘一次，控制内存峰值（2C2G 安全）
            source: 数据源 baostock（默认）/ tencent（腾讯，不封IP）
             target: 仅允许 raw:<src>；旧的 daily 直写路径已下线。
            force_refresh: 忽略已有覆盖日期，重新请求指定证券的完整区间。

        Returns:
            dict: 统计（新增行数/失败数/耗时）
        """
        if not start_date or not end_date:
            raise ValueError(
                "sync_daily 必须显式传入 start_date 和 end_date；"
                "禁止隐式拉取历史区间"
            )

        started_monotonic = time.monotonic()
        if timeout is not None:
            deadline = started_monotonic + max(0.0, float(timeout))
        elif isinstance(deadline, datetime):
            deadline = started_monotonic + max(0.0, (deadline - datetime.now()).total_seconds())

        def _deadline_reached() -> bool:
            return deadline is not None and time.monotonic() >= float(deadline)

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
        selected_symbols = list(symbols)
        from StockInvestmentTool.screener.board import detect_board
        symbol_types = {code: (self.warehouse.instrument_types().get(code) or
                               StockDataFetcher.detect_type(code)) for code in selected_symbols}
        symbol_boards = {code: (detect_board(code) or "unknown") for code in selected_symbols}
        if max_symbols:
            symbols = symbols[:max_symbols]
            selected_symbols = list(symbols)
            symbol_types = {code: symbol_types.get(code, "unknown") for code in selected_symbols}
            symbol_boards = {code: symbol_boards.get(code, "unknown") for code in selected_symbols}

        if target == "daily":
            raise ValueError("sync_daily 不再支持 target='daily'；请使用 Raw Batch + DailyBuilder")
        if target is not None and target != f"raw:{source}":
            raise ValueError(f"sync_daily 只允许写入当前 source 的 Raw Batch: raw:{source}")
        if capture_raw is False:
            raise ValueError("sync_daily 只允许通过 Raw Batch 采集，capture_raw 必须为 True")
        capture_raw = True
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
                                  "include_etf": include_etf, "include_index": include_index,
                                  "universe_source": request_context.get("universe_source") if isinstance(request_context, dict) else None,
                                  "universe_authoritative": request_context.get("universe_authoritative") if isinstance(request_context, dict) else None,
                                  "universe_snapshot_date": request_context.get("universe_snapshot_date") if isinstance(request_context, dict) else None,
                                   },
                 job_run_id=job_run_id, source_name=source,
            )
            raw_writer = self.warehouse.raw.begin_batch(
                source, "stock_daily", datetime.now().strftime("%Y-%m-%d")
            )

        from StockInvestmentTool.warehouse.coverage import CoverageStore
        coverage = CoverageStore(self.warehouse.meta_db_path)

        months = self._month_range(start_date, end_date)
        logger.info("增量同步: %d 标的 × %d 月份 (%s ~ %s)",
                    len(symbols), len(months), start_date, end_date)

        added = 0
        failed: list[str] = []
        skipped_codes: list[str] = []
        timed_out = False
        skipped = 0
        t0 = time.time()

        # Query the persistent index per selected entity type; never scan Raw
        # files during normal incremental capture.
        last_dates = {}
        for kind in sorted({symbol_types.get(code, "unknown") for code in selected_symbols}):
            last_dates.update({
                code: pd.Timestamp(value)
                for code, value in coverage.latest_success_dates(
                    "stock_daily", source,
                    [code for code in selected_symbols if symbol_types.get(code) == kind],
                    kind,
                ).items()
            })
        logger.info("覆盖索引命中: %d 个标的（来自 management.db）", len(last_dates))

        end_ts = pd.Timestamp(end_date)
        for i, code in enumerate(symbols, 1):
            if _deadline_reached():
                timed_out = True
                failed.extend(symbols[i - 1:])
                break
            if progress_callback:
                progress_callback(i - 1, len(symbols), code, "读取日线")
            # 增量判断：该标的自有最后日期 >= end_date → 已覆盖，跳过
            last = last_dates.get(code)
            if not force_refresh and last is not None and last >= end_ts:
                skipped += 1
                skipped_codes.append(code)
                if progress_callback:
                    progress_callback(i, len(symbols), code, "已是最新")
                continue

            # 只拉缺失区间：已有数据的拉 (last_date+1, end_date]，无数据拉全区间
            if not force_refresh and last is not None:
                fetch_start = (last + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                fetch_start = start_date
            if fetch_start > end_date:
                skipped += 1
                skipped_codes.append(code)
                if progress_callback:
                    progress_callback(i, len(symbols), code, "已是最新")
                continue

            try:
                if source == "tencent":
                    remaining = (float(deadline) - time.monotonic()) if deadline is not None else None
                    if remaining is not None and remaining <= 0:
                        raise CollectionTimeout("采集任务已超时")
                    if remaining is None:
                        df = self._fetch_symbol_tencent(code, fetch_start, end_date)
                    else:
                        df = self._fetch_symbol_tencent(code, fetch_start, end_date,
                                                        request_timeout=remaining)
                else:
                    df = self._fetch_symbol(code, fetch_start, end_date)
            except CollectionTimeout:
                timed_out = True
                failed.append(code)
                coverage.record_failure(dataset_name="stock_daily", source_name=source,
                                        entity_type=symbol_types.get(code, "unknown"), entity_id=code,
                                        data_date=end_date, batch_id=batch_id, status="timeout",
                                        error_code="timeout", error_message="采集任务超时")
                failed.extend(symbols[i:])
                logger.warning("采集任务超时，剩余 %d 个标的未处理", len(symbols) - i + 1)
                if progress_callback:
                    progress_callback(i, len(symbols), code, "任务超时")
                break
            except Exception as e:
                failed.append(code)
                coverage.record_failure(dataset_name="stock_daily", source_name=source,
                                        entity_type=symbol_types.get(code, "unknown"), entity_id=code,
                                        data_date=end_date, batch_id=batch_id, status="failed",
                                        error_code=type(e).__name__, error_message=str(e))
                logger.warning("拉取 %s 失败: %s", code, e)
                if _deadline_reached():
                    timed_out = True
                    failed.extend(symbols[i:])
                if progress_callback:
                    progress_callback(i, len(symbols), code, "任务超时" if timed_out else "拉取失败")
                if timed_out:
                    break
                continue
            if df.empty:
                skipped += 1
                skipped_codes.append(code)
                coverage.record_failure(dataset_name="stock_daily", source_name=source,
                                        entity_type=symbol_types.get(code, "unknown"), entity_id=code,
                                        data_date=end_date, batch_id=batch_id, status="empty",
                                        error_code="empty_response", error_message="源返回空数据")
                if progress_callback:
                    progress_callback(i, len(symbols), code, "无新增数据")
                continue
            if capture_raw and not raw_capture_failed:
                try:
                    # Raw is an immutable source-fact layer. Cleaning and unit
                    # conversion happen later in DailyBuilder.
                    raw_writer.append(df.copy())
                except Exception:
                    raw_capture_failed = True
                    raw_writer.abort()
                    raw_writer = None
                    logger.exception("Raw Batch 采集过程中写入失败")
            if not raw_capture_failed:
                dates = [str(value)[:10] for value in df["date"].dropna().unique()]
                coverage.record_success(dataset_name="stock_daily", source_name=source,
                                        entity_type=symbol_types.get(code, "unknown"), entity_id=code,
                                        data_dates=dates, batch_id=batch_id)
            added += len(df)
            if progress_callback:
                progress_callback(i, len(symbols), code, "已入库")
            if i % flush_every == 0 or i == len(symbols):
                logger.info("进度 %d/%d，已入库 %d 行（已落盘）", i, len(symbols), added)

        elapsed = time.time() - t0
        raw_result = None
        if capture_raw and batch_store and batch_id:
            try:
                if raw_writer is None:
                    raise ValueError("Raw Batch 写入失败或没有可写数据")
                raw_result = raw_writer.finish()
                batch_status = "failed" if timed_out and not added else ("partial_success" if failed else "success")
                batch_store.finish(batch_id, success_symbols=len(symbols) - len(failed) - skipped,
                                   failed_symbols=len(failed), skipped_symbols=skipped,
                                   row_count=raw_result["row_count"], raw_path=str(raw_result["path"]),
                                   checksum=raw_result["checksum"], file_size=raw_result["file_size"],
                                    status=batch_status, error_summary="采集任务超时" if timed_out else "",
                                    failure_details=failed)
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
        status = "timeout" if timed_out else ("partial_success" if failed and added else None)
        failed_set = set(failed)
        skipped_set = set(skipped_codes)
        coverage_by_type = {}
        coverage_by_board = {}
        for code in selected_symbols:
            kind = symbol_types.get(code, "unknown")
            board = symbol_boards.get(code, "unknown")
            for bucket, key in ((coverage_by_type, kind), (coverage_by_board, board)):
                item = bucket.setdefault(key, {"expected": 0, "success": 0, "failed": 0, "skipped": 0})
                item["expected"] += 1
                if code in failed_set:
                    item["failed"] += 1
                elif code in skipped_set:
                    item["skipped"] += 1
                else:
                    item["success"] += 1
        if batch_store and batch_id:
            batch_store.update_request_context(batch_id, {
                "asset_type_counts": asset_type_counts,
                "coverage_by_type": coverage_by_type,
                "coverage_by_board": coverage_by_board,
                "failed_symbols": sorted(failed_set),
                "skipped_symbols": sorted(skipped_set),
            })
        return {"added_rows": added, "symbols": len(symbols),
                "failed": failed, "up_to_date": not failed and added == 0,
                "status": status, "timed_out": timed_out,
                "rows": added, "elapsed_sec": round(elapsed, 1),
                "source_batch_id": batch_id, "source_batch_ids": [batch_id] if batch_id else [],
                "raw_capture_failed": raw_capture_failed,
                 "skipped_symbols": skipped,
                 "asset_type_counts": asset_type_counts,
                 "coverage_by_type": coverage_by_type,
                 "coverage_by_board": coverage_by_board,
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
