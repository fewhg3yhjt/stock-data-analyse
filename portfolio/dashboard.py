# -*- coding: utf-8 -*-
"""三页看板数据服务 — 观察池 / 作战仓 / 复盘底账

复用现有能力:
    - 市场状态: market_state.dashboard_market_state（看板 4 态）
    - 支撑/参考价: portfolio.advisor.AdvisorContext / monitor.PriceMonitor
    - 持仓/流水/FIFO: portfolio.manager.PortfolioManager / storage
    - 观察池候选: fundflow 持续流入榜

本模块只做数据聚合（纯逻辑，可单测），渲染交给 web/app.py。
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 指令中文映射（页面二/三共用）
ADVICE_LABELS = {
    "buy_more": "📥加仓",
    "partial_sell": "💡减仓",
    "sell_all": "🚨清仓",
    "hold": "持有不动",
    "adjust_stop": "🔧调止损",
}
# 风险状态分级（按总盈亏比例）
RISK_LEVELS = [
    (10.0, "🟢 正常（允许新开仓）"),
    (-5.0, "⚪ 正常"),
    (-8.0, "🟡 防守（暂停新开仓）"),
    (float("-inf"), "🔴 危机（只止损不开仓）"),
]


def _risk_status(pnl_pct: Optional[float]) -> str:
    if pnl_pct is None:
        return "⚪ 正常"
    for threshold, label in RISK_LEVELS:
        if pnl_pct > threshold:
            return label
    return "🔴 危机（只止损不开仓）"


def _open_instruction(market_state: str, ctx) -> str:
    """观察池开仓指令（设计 4.4 判定表）。"""
    price = getattr(ctx, "current_price", None)
    if market_state == "强多":
        return f"🔥规则C激活：现价{price} 立即建仓20%"
    if market_state == "弱多":
        return f"⏳回踩MA20（{ctx.ma20:.2f}）建仓"
    if market_state == "震荡":
        return f"⏳回落至弱支撑（{ctx.weak_support:.2f}）挂单"
    return "🚫禁买（空头趋势不操作）"


def _risk_light(name: str) -> str:
    """排雷灯（第一版: 名称含 ST/退 → 红灯，其余待财务核验）。"""
    n = (name or "").upper()
    if "ST" in n or "退" in n or "PT" in n:
        return "🔴红灯"
    return "🟢通过"


class DashboardService:
    def __init__(self, manager=None):
        from StockInvestmentTool.portfolio.manager import PortfolioManager
        self.manager = manager or PortfolioManager()

    # ── 页面一 · 观察池 ─────────────────────────────────

    def observe_pool(self, max_candidates: int = 15, use_cache: bool = True,
                     live_refresh: bool = False) -> list[dict]:
        """战前侦察：watchlist 全量 + 资金流持续流入候选 → 每只附 市场状态/支撑/开仓指令。

        按日缓存到 output/data/observe_pool_YYYYMMDD.json：
        全市场资金流拉取 + 逐只指标计算较慢（~20s），当日重复打开直接复用缓存；
        web 传 ?refresh=1 强制重算。
        """
        import hashlib
        import json
        from StockInvestmentTool.config import Config

        today = datetime.now().strftime("%Y%m%d")
        # 缓存带 watchlist 指纹：自选/持仓变化时自动失效重算，同一集合当日复用
        wl_codes = sorted(self.manager.get_watchlist_codes())
        wl_fp = hashlib.md5(",".join(wl_codes).encode()).hexdigest()[:8]
        cache_path = Config.DATA_DIR / f"observe_pool_{today}_{wl_fp}.json"
        if use_cache and cache_path.exists():
            try:
                rows = json.loads(cache_path.read_text(encoding="utf-8"))
                logger.info("观察池缓存命中: %s (%d 行)", cache_path.name, len(rows))
                return self._refresh_prices(rows) if live_refresh else self._apply_local_snapshots(rows)
            except Exception as e:
                logger.warning("观察池缓存读取失败: %s", e)

        seen = set()
        items: list[dict] = []
        today = datetime.now().strftime("%Y-%m-%d")
        for w in self.manager.get_watchlist():
            at = w.added_time or today
            items.append({"code": w.stock_code, "name": w.stock_name,
                          "notes": w.notes or "", "added_time": at,
                          "pending": at > today})
        for cand in self._flow_candidates(max_candidates):
            if cand["code"] not in seen:
                items.append(cand)
        # watchlist 优先，候选去重
        rows = []
        for item in items:
            code = (item.get("code") or "").lower()
            if not code or code in seen:
                continue
            seen.add(code)
            if item.get("pending"):
                rows.append({
                    "code": code,
                    "name": item.get("name") or code,
                    "notes": item.get("notes") or "",
                    "added_time": item.get("added_time") or "",
                    "pending": True,
                    "price": None, "market_state": "待观察",
                    "risk_light": _risk_light(item.get("name") or ""),
                    "instruction": f"观察起点 {item.get('added_time')} 晚于当天，待开始后生成指标",
                    "ok": False,
                })
                continue
            try:
                rows.append(self._observe_one(item))
            except Exception as e:
                logger.warning("观察池 %s 计算失败: %s", code, e)
                rows.append(self._observe_fallback(item, str(e)))
        try:
            cache_path.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
            logger.info("观察池缓存已写入: %s", cache_path.name)
        except Exception as e:
            logger.warning("观察池缓存写入失败: %s", e)
        return self._refresh_prices(rows) if live_refresh else self._apply_local_snapshots(rows)

    def watch_pool(self, *, refresh: bool = False) -> list[dict]:
        """Merge watchlist, strategy observations, simulations, and holdings."""
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher

        def key(code: str) -> str:
            return StockDataFetcher.normalize_code(code).replace(".", "").lower()

        watchlist = self.manager.get_watchlist()
        simulations = self.manager.get_simulations()
        holdings = self.manager.storage.get_open_positions()
        observations = self.observe_pool(use_cache=not refresh)
        merged: dict[str, dict] = {}

        def entry(code: str, name: str = "") -> dict:
            normalized = key(code)
            item = merged.setdefault(normalized, {
                "code": normalized, "name": name or normalized, "sources": [],
                "watch": None, "simulation": None, "holding": None,
                "stock_type": "B",
                "observation": None, "next_action": "none",
            })
            if name:
                item["name"] = name
            return item

        for watch in watchlist:
            item = entry(watch.stock_code, watch.stock_name)
            item["watch"] = watch.to_dict()
            if watch.asset_type == "etf":
                item["stock_type"] = "E"
            if watch.source not in item["sources"]:
                item["sources"].append(watch.source)
        for observation in observations:
            item = entry(observation.get("code", ""), observation.get("name", ""))
            item["observation"] = observation
            if observation.get("code") and "strategy" not in item["sources"] and not item["watch"]:
                item["sources"].append("strategy")
        for simulation in simulations:
            item = entry(simulation.stock_code, simulation.stock_name)
            item["simulation"] = simulation.to_dict()
            item["stock_type"] = simulation.stock_type or item["stock_type"]
        for holding in holdings:
            item = entry(holding.stock_code, holding.stock_name)
            item["holding"] = holding.to_dict()
            item["stock_type"] = holding.stock_type or item["stock_type"]
            if "holding" not in item["sources"]:
                item["sources"].append("holding")
        for item in merged.values():
            if item["holding"]:
                item["next_action"] = "review"
            elif item["simulation"]:
                item["next_action"] = "buy"
            elif item["watch"]:
                item["next_action"] = "simulate"
            elif item["observation"]:
                item["next_action"] = "observe"
            else:
                item["next_action"] = "none"
        return sorted(merged.values(), key=lambda item: (item["name"], item["code"]))

    def _apply_local_snapshots(self, rows: list[dict]) -> list[dict]:
        """Overlay scheduled local data without performing network I/O."""
        if not rows:
            return rows
        local = self._local_enhance([r.get("code", "") for r in rows])
        for row in rows:
            quote = local.get(self._normalize_quote_code(row.get("code", "")))
            if not quote:
                row.setdefault("data_source", "technical_cache")
                continue
            for field, value in quote.items():
                if value is not None:
                    row[field] = value
            row["data_source"] = quote.get("data_source", "scheduled_snapshot")
        return rows

    @staticmethod
    def _normalize_quote_code(code: str) -> str:
        return str(code or "").lower().replace(".", "")

    def _local_enhance(self, codes: list[str]) -> dict[str, dict]:
        """Read minute snapshots first, then local online snapshots as fallback."""
        from StockInvestmentTool.warehouse.storage import Warehouse
        import pandas as pd

        wanted = {self._normalize_quote_code(code) for code in codes if code}
        out: dict[str, dict] = {}
        warehouse = Warehouse()
        minute_store = warehouse.minute_store()
        minute_days = minute_store.days()
        if minute_days:
            frame = minute_store.read(minute_days[-1])
            if not frame.empty and "time" in frame.columns:
                frame["_parsed_time"] = pd.to_datetime(frame["time"], errors="coerce")
                frame = frame.dropna(subset=["_parsed_time"]).sort_values("_parsed_time")
                for code, group in frame.groupby("code"):
                    key = self._normalize_quote_code(code)
                    if key not in wanted:
                        continue
                    latest = group.iloc[-1]
                    out[key] = {"price": float(latest["close"]),
                                "snapshot_time": str(latest["time"])[:19],
                                "data_source": "minute_snapshot"}
        if len(out) < len(wanted):
            latest_path = None
            for day in sorted(warehouse.online_dir.iterdir(), reverse=True):
                if day.is_dir():
                    paths = sorted(day.glob("snapshot_*.csv"))
                    if paths:
                        latest_path = paths[-1]
                        break
            if latest_path:
                try:
                    frame = pd.read_csv(latest_path, encoding="utf-8-sig")
                    for _, quote in frame.iterrows():
                        key = self._normalize_quote_code(quote.get("code", ""))
                        if key not in wanted:
                            continue
                        target = out.setdefault(key, {})
                        for field in ("price", "change_pct", "volume", "amount_wan", "turnover",
                                      "vol_ratio", "pe_ttm", "pb", "snapshot_time"):
                            if field in quote and pd.notna(quote[field]) and field not in target:
                                target[field] = quote[field]
                        target.setdefault("data_source", "online_snapshot")
                except Exception as exc:
                    logger.warning("本地在线快照读取失败: %s", exc)
        return out

    def _refresh_prices(self, rows: list[dict]) -> list[dict]:
        """每次打开页面用腾讯实时报价刷新现价/涨跌幅，并合并增强字段。

        现价/涨跌实时拉腾讯；量/换手/量比/PE/PB 等增强字段优先当日
        warehouse/online 快照（10min 采集），缺省用腾讯同批字段。
        腾讯失败时降级保留原价。
        """
        if not rows:
            return rows
        try:
            from StockInvestmentTool.screener.board import normalize
            # 观察池行代码可能无前缀（601899），增强 dict 以带前缀 key（sh601899）存储
            codes = [normalize(r["code"]) for r in rows]
            enhance = self._realtime_enhance(codes)
            updated = 0
            for r in rows:
                q = enhance.get(normalize(r["code"]))
                if not q:
                    continue
                if q.get("price") not in (None, 0):
                    r["price"] = round(float(q["price"]), 2)
                    updated += 1
                for f in ("change_pct", "volume", "amount_wan", "turnover",
                          "vol_ratio", "pe_ttm", "pb", "snapshot_time"):
                    if q.get(f) is not None:
                        r[f] = q[f]
            logger.info("观察池现价已实时刷新 %d/%d 只", updated, len(rows))
        except Exception as e:
            logger.warning("观察池现价实时刷新失败(降级用缓存价): %s", e)
        return rows

    def _realtime_enhance(self, codes: list[str]) -> dict:
        """批量取实时增强字段（现价/涨跌/量/换手/量比/PE/PB 等）。

        现价/涨跌实时拉腾讯；其余增强字段优先用当日 warehouse/online 快照
        （由 WAREHOUSE_ONLINE_SNAPSHOT 每 10 分钟采集），缺省降级到腾讯同批字段，
        再无则留空。返回 key=无点代码 的 dict。
        """
        from StockInvestmentTool.screener.board import normalize
        from StockInvestmentTool.screener.sources import tencent_quotes
        from StockInvestmentTool.warehouse.storage import Warehouse

        norm_codes = [normalize(c) for c in codes]
        out: dict[str, dict] = {}

        # 腾讯实时（现价/涨跌为主，增强字段兜底）
        try:
            quotes = tencent_quotes(norm_codes)
            for _, q in quotes.iterrows():
                key = (q.get("code") or "").lower().replace(".", "")
                if not key:
                    continue
                row = out.setdefault(key, {})
                if q.get("price") not in (None, 0):
                    row["price"] = round(float(q["price"]), 2)
                if q.get("change_pct") is not None:
                    row["change_pct"] = round(float(q["change_pct"]), 2)
                for f in ("amount_wan", "turnover", "vol_ratio", "pe_ttm", "pb",
                          "volume", "high", "low", "open", "prev_close",
                          "amplitude", "float_mcap", "total_mcap"):
                    v = q.get(f)
                    if v is not None:
                        row[f] = round(float(v), 2) if f != "volume" else round(float(v), 0)
        except Exception as e:
            logger.warning("腾讯实时增强拉取失败: %s", e)

        # 当日 warehouse online 快照（10min 采集）补充增强字段
        try:
            w = Warehouse()
            today = datetime.now().strftime("%Y-%m-%d")
            snaps = w.online_snapshots(today)
            if snaps:
                import pandas as pd
                latest = pd.read_csv(snaps[-1], encoding="utf-8-sig")
                for _, r in latest.iterrows():
                    key = str(r.get("code", "")).lower().replace(".", "")
                    if not key:
                        continue
                    row = out.setdefault(key, {})
                    for f in ("price", "change_pct", "turnover", "vol_ratio",
                              "pe_ttm", "pb", "amount_wan", "high", "low", "open"):
                        if f in r and f not in row:
                            try:
                                v = float(r[f])
                                if v == v:
                                    row[f] = round(v, 2)
                            except (TypeError, ValueError):
                                pass
                    if r.get("snapshot_time"):
                        row["snapshot_time"] = str(r["snapshot_time"])[:19]
        except Exception as e:
            logger.warning("当日在线快照读取失败: %s", e)

        return out

    def _flow_candidates(self, top_n: int) -> list[dict]:
        """从 fundflow 持续流入榜取候选（只取代码/名称/净额，不阻塞主流程）。

        当日缓存 + 短超时：全市场资金流拉取（同花顺）网络不稳时可能卡死，
        这里缓存当日结果并用线程超时保护，拉不到就降级返回空（不影响观察池主体）。
        """
        if top_n <= 0:
            return []
        import json
        from StockInvestmentTool.config import Config

        today = datetime.now().strftime("%Y%m%d")
        cache_path = Config.DATA_DIR / f"fundflow_candidates_{today}.json"
        if cache_path.exists():
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("资金流候选缓存读取失败: %s", e)

        def _fetch() -> list[dict]:
            from StockInvestmentTool.fundflow import analysis, sources

            stk_now = sources.fetch_stock("now")
            stk_3d = sources.fetch_stock("3d")
            res = analysis.stock_analysis(stk_now, stk_3d, top=top_n)
            out = []
            for _, r in res["持续流入榜"].head(top_n).iterrows():
                out.append({"code": r["code"], "name": r["name"],
                            "notes": f"资金持续流入(3日{r.get('net_days'):.1f}亿)"})
            return out

        out = []
        try:
            import threading
            holder: dict[str, list[dict]] = {"rows": []}
            def _worker():
                try:
                    holder["rows"] = _fetch()
                except Exception as e:
                    logger.warning("资金流候选拉取失败: %s", e)
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            t.join(timeout=15)
            if t.is_alive():
                logger.warning("资金流候选拉取超时(>15s)，跳过候选（仅显示 watchlist）")
            else:
                out = holder["rows"]
                try:
                    cache_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
        except Exception as e:
            logger.warning("资金流候选异常: %s", e)
        return out

    def stock_dual_view(self, code: str, days: int = 750,
                        transaction_points: Optional[list[dict]] = None) -> dict:
        """单只股票的「天周期历史 + 盘中快照」双视图数据（后端打通）。

        框1 天周期历史: OHLCV 取自 warehouse daily 分区；均线 MA5/10/20/60 直接
            复用 warehouse indicators 分区（离线批处理已算好，不在此现算）。
        框2 盘中快照:   warehouse online 当日最新快照（实时价/换手/量比）

        Returns:
            dict: {code, daily_history: {dates, closes, ...}, intraday: {...}}
        """
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher
        from StockInvestmentTool.warehouse.storage import Warehouse

        norm = StockDataFetcher.normalize_code(code)
        code_nodot = norm.replace(".", "")
        result = {"code": code_nodot, "name": code_nodot,
                  "daily_history": {}, "intraday": {}}

        # 标的名称（meta 清单）
        try:
            w = Warehouse()
            instrument = w.get_instrument(code_nodot)
            if instrument and instrument.get("name"):
                result["name"] = instrument["name"]
        except Exception as e:
            logger.warning("标的名称读取失败 %s: %s", code, e)

        # 框1：天周期历史（DuckDB 单查询，替代逐月 read_parquet —— FR-1.4/P1）
        try:
            from StockInvestmentTool.datasource.base import WarehouseSource
            import pandas as _pd

            hdf = WarehouseSource().fetch_daily_series(code_nodot, days)
            if hdf is not None and not hdf.empty:
                hdf = hdf.tail(days).reset_index(drop=True)
                closes_s = _pd.to_numeric(hdf["close"], errors="coerce")

                def _number_list(values, decimals=2):
                    """把可选行情列安全转成 JSON 数值，缺失值保留 None。"""
                    return [
                        round(float(value), decimals) if _pd.notna(value) else None
                        for value in values
                    ]

                # 复用 indicators 分区里已预计算的均线（与 daily 同源、按日期对齐），
                # 指标是离线批处理算好落盘的，不在此重复现算；单指标缺失时按列现算补上。
                mas = {}
                try:
                    _tmp = Warehouse()
                    ind_orig = _tmp.read_indicator_code(code_nodot, days=days)
                    if ind_orig is not None:
                        ind_orig = ind_orig.sort_values("date").reset_index(drop=True)
                        keep = [c for c in ("ma5", "ma10", "ma20", "ma60") if c in ind_orig.columns]
                        if keep:
                            merged = _pd.merge(
                                hdf[["date"]], ind_orig[["date"] + keep],
                                on="date", how="left"
                            ).sort_values("date").reset_index(drop=True)
                            for n, col in {"ma5": "ma5", "ma10": "ma10",
                                           "ma20": "ma20", "ma60": "ma60"}.items():
                                if col in keep:
                                    s = _pd.to_numeric(merged[col], errors="coerce")
                                    mas[n] = [round(float(x), 2) if x == x else None for x in s]
                except Exception as e:
                    logger.warning("指标分区读取失败 %s: %s", code, e)

                # 缺失的均线按列回退本地现算（保证 MA 可用）
                for n in (5, 10, 20, 60):
                    key = f"ma{n}"
                    if key in mas:
                        continue
                    ma = closes_s.rolling(n).mean()
                    mas[key] = [round(float(x), 2) if x == x else None for x in ma]

                result["daily_history"] = {
                    "dates": [str(d)[:10] for d in hdf["date"]],
                    "closes": _number_list(hdf["close"]),
                    "opens": _number_list(hdf.get("open", _pd.Series([None]*len(hdf)))),
                    "highs": _number_list(hdf.get("high", _pd.Series([None]*len(hdf)))),
                    "lows": _number_list(hdf.get("low", _pd.Series([None]*len(hdf)))),
                    "volumes": _number_list(hdf["volume"], 0),
                    "amounts": _number_list(hdf.get("amount", _pd.Series([None]*len(hdf)))),
                    "turns": _number_list(hdf.get("turn", _pd.Series([None]*len(hdf)))),
                    "pe": _number_list(hdf.get("pe_ttm", _pd.Series([None]*len(hdf)))),
                    "pb": _number_list(hdf.get("pb_mrq", _pd.Series([None]*len(hdf)))),
                    "mas": mas,
                    "transaction_points": self._transaction_points(hdf, transaction_points),
                }
        except Exception as e:
            logger.warning("天周期历史读取失败 %s: %s", code, e)

        # 框2：优先读取独立 minute 分区；没有分钟数据才回退 online 快照。
        # 数据源：warehouse.online_snapshots（当前为每日快照；
        #   后续接入分钟级采集后，仅需切换此处数据源，前端无需改动）
        try:
            import pandas as pd
            today = datetime.now().strftime("%Y-%m-%d")
            w = Warehouse()
            from StockInvestmentTool.datasource.base import WarehouseSource

            minute = WarehouseSource(warehouse=w).fetch_minute_series(code_nodot, today)
            if not minute.empty:
                last = minute.iloc[-1]
                result["intraday"] = {
                    "price": round(float(last["close"]), 2),
                    "snapshot_time": str(last["time"])[:19],
                    "source": "tencent_minute",
                }
                result["intraday_trend"] = {
                    "day": today,
                    "times": [str(v)[:16] for v in minute["time"]],
                    "prices": [round(float(v), 2) for v in minute["close"]],
                    "source": "tencent_minute",
                }
                return result
            snaps_today = w.online_snapshots(today)
            if snaps_today:
                latest = pd.read_csv(snaps_today[-1], encoding="utf-8-sig")
                row = latest[latest["code"] == code_nodot]
                if len(row):
                    r = row.iloc[0]
                    result["intraday"] = {
                        "price": round(float(r["price"]), 2) if r.get("price") == r.get("price") else None,
                        "change_pct": round(float(r["change_pct"]), 2) if r.get("change_pct") == r.get("change_pct") else None,
                        "turnover": round(float(r["turnover"]), 2) if r.get("turnover") == r.get("turnover") else None,
                        "vol_ratio": round(float(r["vol_ratio"]), 2) if r.get("vol_ratio") == r.get("vol_ratio") else None,
                        "pe_ttm": round(float(r["pe_ttm"]), 2) if r.get("pe_ttm") == r.get("pe_ttm") else None,
                        "pb": round(float(r["pb"]), 2) if r.get("pb") == r.get("pb") else None,
                        "snapshot_time": str(r.get("snapshot_time", ""))[:19],
                    }
            # 走势序列：当日优先；无当日快照则回退到最近一个有快照的交易日
            snaps = snaps_today
            snap_day = today
            if not snaps:
                for off in range(1, 15):
                    d = (datetime.now() - timedelta(days=off)).strftime("%Y-%m-%d")
                    s2 = w.online_snapshots(d)
                    if s2:
                        snaps = s2
                        snap_day = d
                        break
            if snaps:
                times, prices = [], []
                for sp in snaps:
                    try:
                        sdf = pd.read_csv(sp, encoding="utf-8-sig")
                    except Exception:
                        continue
                    if "code" not in sdf.columns:
                        continue
                    hit = sdf[sdf["code"] == code_nodot]
                    if not len(hit):
                        continue
                    r = hit.iloc[0]
                    p = r.get("price")
                    t = str(r.get("snapshot_time", ""))[:16]
                    if p == p and t:  # 非 NaN 且有时间
                        times.append(t)
                        prices.append(round(float(p), 2))
                if times:
                    result["intraday_trend"] = {"day": snap_day, "times": times, "prices": prices}
        except Exception as e:
            logger.warning("盘中快照读取失败 %s: %s", code, e)

        return result

    @staticmethod
    def _transaction_points(hdf, transactions: Optional[list[dict]]) -> list[dict]:
        """把持仓交易流水映射到日 K 日期和价格，供图表叠加标记。"""
        if not transactions:
            return []
        dates = [str(value)[:10] for value in hdf["date"]]
        points = []
        for txn in transactions:
            trade_date = str(txn.get("date") or "")[:10]
            price = txn.get("price")
            if not trade_date or price in (None, 0):
                continue
            matching = [i for i, value in enumerate(dates) if value >= trade_date]
            if not matching:
                continue
            points.append({
                "index": matching[0], "date": dates[matching[0]], "trade_date": trade_date,
                "price": round(float(price), 4), "shares": round(float(txn.get("shares") or 0), 2),
                "amount": round(float(txn.get("amount") or 0), 2),
                "pnl": round(float(txn.get("pnl") or 0), 2),
                "type": txn.get("trans_type") or "", "reason": txn.get("reason") or "",
            })
        return points

    def stock_chart_series(self, code: str, period: str = "day",
                           days: int = 120,
                           cost_price: Optional[float] = None,
                           metrics: Optional[list[str]] = None) -> dict:
        """单只股票的可视化序列（ECharts 用）。

        支持日/周/月重采样 + 指标体系 + 收益率双线（成本收益/价格收益）。

        Args:
            code: 股票代码
            period: day/week/month（重采样粒度）
            days: 数据长度（交易日）
            cost_price: 持仓成本（用于成本收益率，None=只算价格收益）
            metrics: 要展示的指标名（None=收盘+MA20+MA60）

        Returns:
            {code, dates, closes, metrics:[{name,key,data}],
             ret_cost, ret_price, avg_cost}
        """
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher
        from StockInvestmentTool.datasource.base import WarehouseSource
        from StockInvestmentTool.warehouse.storage import Warehouse

        norm = StockDataFetcher.normalize_code(code)
        code_nodot = norm.replace(".", "")
        if metrics is None:
            metrics = ["ma20", "ma60"]

        # 读 warehouse 日线（DuckDB 单查询，替代逐月 read_parquet —— FR-1.4/P1）
        try:
            kline = WarehouseSource().fetch_daily_series(code_nodot, days * 2)
        except Exception as e:
            logger.warning("图表序列读取失败 %s: %s", code, e)
            kline = None
        if kline is None or kline.empty:
            return {"code": code_nodot, "dates": [], "closes": [],
                    "metrics": [], "ret_cost": [], "ret_price": [],
                    "avg_cost": cost_price}

        # 指标统一从 indicators 分区读取（与个股详情同源），缺失时回退现算
        import pandas as pd
        ind_series = {}
        try:
            ind_orig = Warehouse().read_indicator_code(code_nodot, days=days * 2)
            if ind_orig is not None and not ind_orig.empty:
                ind_orig = ind_orig.sort_values("date").reset_index(drop=True)
                merged = pd.merge(kline[["date"]], ind_orig, on="date", how="left")
                for name in metrics:
                    if name in merged.columns:
                        ind_series[name] = pd.to_numeric(merged[name], errors="coerce")
        except Exception as e:
            logger.warning("指标分区读取失败 %s: %s", code, e)
        missing = [name for name in metrics if name not in ind_series]
        if missing:
            from StockInvestmentTool.indicators.engine import IndicatorRegistry
            ind_series.update(IndicatorRegistry().compute(kline, missing))

        # 重采样（日/周/月）
        if period == "week":
            kline["_period"] = kline["date"].dt.to_period("W")
        elif period == "month":
            kline["_period"] = kline["date"].dt.to_period("M")
        else:
            kline["_period"] = kline["date"].dt.to_period("D")

        agg = kline.groupby("_period").agg(
            close=("close", "last"),
            start_close=("close", "first"),
            _date=("date", "last"),
        ).reset_index().sort_values("_date").tail(days)

        dates = [str(d)[:10] for d in agg["_date"]]
        closes = [round(float(x), 2) for x in agg["close"]]

        # 指标序列（按重采样粒度重采样）
        metric_series = []
        for name in metrics:
            if name not in ind_series:
                continue
            s = ind_series[name]
            if s is None or s.empty:
                continue
            s_df = pd.DataFrame({"date": kline["date"], "val": s.values})
            if period == "week":
                s_df["_p"] = s_df["date"].dt.to_period("W")
            elif period == "month":
                s_df["_p"] = s_df["date"].dt.to_period("M")
            else:
                s_df["_p"] = s_df["date"].dt.to_period("D")
            s_agg = s_df.groupby("_p")["val"].last().reindex(agg["_period"]).tail(days)
            vals = [round(float(x), 2) if x == x else None for x in s_agg]
            metric_series.append({"name": name, "key": name, "data": vals})

        # 收益率双线
        start_close = float(agg["start_close"].iloc[0]) if len(agg) else 1
        ret_price = [round((float(c) / start_close - 1) * 100, 2) if start_close else None
                     for c in closes]
        ret_cost = []
        if cost_price and cost_price > 0:
            ret_cost = [round((float(c) / float(cost_price) - 1) * 100, 2) for c in closes]

        return {"code": code_nodot, "dates": dates, "closes": closes,
                "metrics": metric_series,
                "ret_cost": ret_cost, "ret_price": ret_price,
                "avg_cost": cost_price}

    def _stock_analysis(self, code: str, added_time: str = "") -> dict:
        """统一个股技术面分析（观察列表 _observe_one 与详情 stock_detail 共用）。

        拉日K（可选按观察起点 added_time 截取）→ compute_context → market_state，
        汇总技术面字段。数据源统一：warehouse 日线 + advisor 参考价 + 市场状态。

        Returns:
            {"norm", "kline", "ctx", "market_state",
             "basic": {code, market_state, weak_support, strong_support,
                       ma20, ma60, trend, year_high}}
        """
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher
        from StockInvestmentTool.portfolio.monitor import PriceMonitor
        from StockInvestmentTool.strategy.market_state import dashboard_market_state

        norm = StockDataFetcher.normalize_code(code)
        monitor = PriceMonitor()
        kline, dividend_anchor = monitor.fetch_context_data(norm)
        if added_time:
            import pandas as _pd
            mask = _pd.to_datetime(kline["date"]) >= _pd.to_datetime(added_time)
            filtered = kline[mask].reset_index(drop=True)
            if len(filtered) > 0:
                kline = filtered
        ctx = self.manager.advisor.compute_context(kline, dividend_anchor)
        market_state = dashboard_market_state(kline)
        basic = {
            "code": norm,
            "market_state": market_state,
            "weak_support": round(ctx.weak_support, 2) if ctx.weak_support else None,
            "strong_support": round(ctx.strong_support, 2) if ctx.strong_support else None,
            "ma20": round(ctx.ma20, 2) if ctx.ma20 else None,
            "ma60": round(ctx.ma60, 2) if ctx.ma60 else None,
            "trend": ctx.trend,
            "year_high": round(ctx.year_high, 2) if ctx.year_high else None,
        }
        return {"norm": norm, "kline": kline, "ctx": ctx,
                "market_state": market_state, "basic": basic}

    def stock_detail(self, code: str, kind: str = "watch",
                     entry: Optional[dict] = None) -> dict:
        """统一个股详情聚合（观察/自选/持仓共用）。

        返回单只股票全套数据：
          - basic: 基础指标（现价/涨跌/量/成交额/换手/量比/PE/PB/MA20/市场状态/支撑/点位）
          - daily_history / intraday: 天级K线 + 盘中实时快照（双视图）
          - lines: 止盈止损点位（图表叠加）
          - returns: 收益（按 kind + entry）
                watch    → 模拟收益：基于可调入场价（entry 或默认观察起点价）
                position → 实际收益：基于实际成本×份额

        Args:
            code: 股票代码
            kind: "watch" / "position"
            entry: {"date","price"} 模拟入场点（自选用，可空）
        """
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher

        norm = StockDataFetcher.normalize_code(code)
        code_nodot = norm.replace(".", "")

        # 技术面（与观察列表共用统一入口）
        an = self._stock_analysis(code)
        kline, ctx = an["kline"], an["ctx"]
        basic = dict(an["basic"])

        # 实时增强字段（现价/涨跌/量/换手/量比/PE/PB）
        rt = self._realtime_enhance([code_nodot]).get(code_nodot, {})
        basic.update({
            "price": rt.get("price"),
            "change_pct": rt.get("change_pct"),
            "volume": rt.get("volume"),
            "amount_wan": rt.get("amount_wan"),
            "turnover": rt.get("turnover"),
            "vol_ratio": rt.get("vol_ratio"),
            "pe_ttm": rt.get("pe_ttm"),
            "pb": rt.get("pb"),
            "snapshot_time": rt.get("snapshot_time"),
            "high": rt.get("high"),
            "low": rt.get("low"),
            "open": rt.get("open"),
            "prev_close": rt.get("prev_close"),
            "amplitude": rt.get("amplitude"),
            "float_mcap": rt.get("float_mcap"),
            "total_mcap": rt.get("total_mcap"),
        })

        detail_transactions = []
        if kind == "position":
            for candidate in self.manager.storage.get_open_positions():
                if StockDataFetcher.normalize_code(candidate.stock_code) == norm:
                    detail_transactions.extend(
                        txn.to_dict() for txn in self.manager.storage.get_transactions(candidate.id)
                    )
        dual = self.stock_dual_view(norm, transaction_points=detail_transactions)

        lines = []
        if ctx.weak_support:
            lines.append({"name": "弱支撑", "value": round(ctx.weak_support, 2), "color": "#e67e22"})
        if ctx.strong_support:
            lines.append({"name": "强支撑", "value": round(ctx.strong_support, 2), "color": "#e67e22"})
        if ctx.year_high:
            lines.append({"name": "止盈预警(前高90%)", "value": round(ctx.year_high * 0.9, 2), "color": "#28a745"})
            lines.append({"name": "止盈硬上限", "value": round(ctx.year_high * 1.05, 2), "color": "#1a73e8"})

        returns = None
        if kind == "position":
            pos = None
            for p in self.manager.storage.get_open_positions():
                if StockDataFetcher.normalize_code(p.stock_code) == norm:
                    pos = p
                    break
            if pos is not None:
                cb = self.manager.cost_basis(pos.id)
                returns = self._compute_return(kline, start_date=pos.buy_date,
                                               cost_price=cb["cost_price"], shares=cb["shares"])
                basic["entry_price"] = cb["cost_price"]
                basic["entry_date"] = pos.buy_date
                basic["shares"] = cb["shares"]
                # 历史收益曲线使用收盘价；详情顶部的持仓收益使用同一轮实时价。
                live_price = basic.get("price")
                if returns is not None and live_price not in (None, 0):
                    live_price = float(live_price)
                    cost_price = float(cb["cost_price"])
                    shares = float(cb["shares"])
                    returns["latest_price"] = round(live_price, 4)
                    returns["latest_price_source"] = basic.get("snapshot_time") or "realtime"
                    returns["ret_pct_latest"] = round((live_price / cost_price - 1) * 100, 2) if cost_price else None
                    returns["ret_amount_latest"] = round((live_price - cost_price) * shares, 2)
        else:
            entry_price = None
            entry_date = ""
            if entry and entry.get("price"):
                entry_price = float(entry["price"])
                entry_date = entry.get("date") or ""
            elif entry and entry.get("date"):
                entry_date = entry["date"]
                entry_price = self._price_on_date(kline, entry_date)
            else:
                wl = None
                for w in self.manager.get_watchlist():
                    if w.stock_code and StockDataFetcher.normalize_code(w.stock_code) == norm:
                        wl = w
                        break
                if wl and wl.sim_entry and wl.sim_entry.get("price"):
                    entry_price = float(wl.sim_entry["price"])
                    entry_date = wl.sim_entry.get("date") or ""
                elif wl and wl.added_time:
                    entry_date = wl.added_time
                    entry_price = self._price_on_date(kline, wl.added_time)
            if entry_price and entry_price > 0:
                returns = self._compute_return(kline, start_date=entry_date or "",
                                               cost_price=entry_price, shares=0)
            basic["entry_price"] = entry_price
            basic["entry_date"] = entry_date or ""

        return {
            "code": norm,
            "basic": basic,
            "daily_history": dual.get("daily_history", {}),
            "intraday": dual.get("intraday", {}),
            "intraday_trend": dual.get("intraday_trend", {}),
            "lines": lines,
            "returns": returns,
        }

    @staticmethod
    def _price_on_date(kline, date: str) -> Optional[float]:
        """取 kline 中指定日期（或其后首个交易日）的收盘价。"""
        import pandas as _pd
        if kline is None or kline.empty or not date:
            return None
        try:
            dates = _pd.to_datetime(kline["date"])
            sd = _pd.to_datetime(date)
            after = dates[dates >= sd]
            if len(after) == 0:
                return None
            idx = dates[dates == after.iloc[0]].index[0]
            return round(float(kline.loc[idx, "close"]), 2)
        except Exception:
            return None

    @staticmethod
    def _compute_return(kline, start_date: str, cost_price, shares: float = 0.0):
        """算收益明细（累计收益率序列），附最后值。"""
        from StockInvestmentTool.analysis.returns import compute_returns
        if kline is None or kline.empty:
            return None
        r = compute_returns(kline, start_date=start_date,
                            cost_price=cost_price, shares=shares)
        if r is None or r.empty:
            return None
        last = r.iloc[-1]
        return {
            "dates": [str(d)[:10] for d in r["date"]],
            "ret_pct": [round(float(x), 2) if x == x else None for x in r["ret_pct"]],
            "close": [round(float(x), 2) if x == x else None for x in r["close"]],
            "latest_close": round(float(last["close"]), 2),
            "ret_pct_latest": round(float(last["ret_pct"]), 2),
            "ret_amount_latest": round(float(last["ret_amount"]), 2),
            "cost_price": cost_price,
        }

    def position_returns(self, position_id: int) -> dict:
        """持仓收益工作台：实际持仓 / 模拟方案 / 大盘基准 / 买入持有 四条收益曲线。

        统一以建仓日为起点，四条曲线共用同一日期轴。
        模拟方案用当前策略从建仓日回测；大盘基准按股票前缀映射宽基指数。
        """
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher
        import pandas as pd

        p = self.manager.storage.get_position(position_id)
        if p is None:
            raise ValueError(f"持仓不存在: {position_id}")
        code = p.stock_code
        norm = StockDataFetcher.normalize_code(code)
        cb = self.manager.cost_basis(position_id)
        buy_date = p.buy_date

        txns = [txn.to_dict() for txn in self.manager.storage.get_transactions(position_id)]

        # ① 实际持仓收益：逐日重放真实交易流水，后续加仓不会反向影响过去。
        an = self._stock_analysis(code)
        kline = an["kline"]
        kline, latest_intraday = self._overlay_latest_minute(kline, norm)
        actual = self._actual_position_returns(kline, txns)
        dates = actual["dates"]
        actual_series = actual["series"]

        # ④ 买入持有：从第一笔真实买入成交价（含买入费用）起算，不模拟后续操作。
        buy_hold_series = []
        first_buy = next((t for t in txns if t.get("trans_type") == "buy"), None)
        if kline is not None and not kline.empty:
            mask = pd.to_datetime(kline["date"]) >= pd.Timestamp(str(first_buy.get("date") if first_buy else buy_date)[:10])
            window = kline[mask].reset_index(drop=True)
            if not window.empty:
                first_amount = float(first_buy.get("amount") or 0) if first_buy else 0.0
                first_fee = float(first_buy.get("fee") or 0) if first_buy else 0.0
                first_shares = float(first_buy.get("shares") or 0) if first_buy else 0.0
                base = (first_amount + first_fee) / first_shares if first_shares else float(window["close"].iloc[0])
                base = base or 1.0
                buy_hold_by_date = {str(d)[:10]: round((float(c) / base - 1) * 100, 2)
                                    for d, c in zip(window["date"], window["close"])}
                buy_hold_series = [buy_hold_by_date.get(d) for d in dates]
        if len(buy_hold_series) != len(dates):
            buy_hold_series = [None] * len(dates)

        # ② 模拟方案收益：用当前策略从建仓日回测
        simulation_series = []
        try:
            from StockInvestmentTool.backtest.engine import BacktestEngine
            from StockInvestmentTool.portfolio.models import load_snapshot_scheme
            scheme = load_snapshot_scheme(p.scheme_snapshot)
            if scheme is None:
                scheme = SchemeRegistry().get(p.scheme_name)
            # 模拟策略仍按日线成交逻辑运行；如分钟数据补出了当天，
            # 将该日权益向前值对齐，避免把盘中快照伪装成一笔日线交易。
            simulation_kline = an["kline"]
            engine = BacktestEngine(
                simulation_kline, initial_cash=scheme.backtest.initial_cash,
                stock_type=p.stock_type, scheme=scheme,
            )
            trailing_rule = scheme.rule("sell", "right_side_trailing")
            trailing_params = trailing_rule.params if trailing_rule is not None else {}
            trail_threshold = float(trailing_params.get(
                "drawdown_stop", getattr(scheme.risk, "drawdown_stop", 0.05)
            ) or 0.05)
            result = engine.run_custom(
                trail_threshold=trail_threshold,
                offset=0.0,
                trade_start_date=buy_date,
            )
            equity = (result.get("backtest") or {}).get("equity_curve") or []
            eq_pairs = []
            for item in equity:
                if isinstance(item, dict):
                    eq_pairs.append((str(item.get("date"))[:10], float(item.get("total_asset"))))
            if eq_pairs:
                sim_dates = {d for d, _ in eq_pairs}
                sim_by_date = dict(eq_pairs)
                first_cash = eq_pairs[0][1] if eq_pairs else 0.0
                # 与统一日期轴对齐；trade_start_date 已保证建仓日前不交易。
                last_val = None
                simulation_series = []
                start_equity = next((v for d, v in eq_pairs if d >= dates[0]), None) if dates else None
                start_equity = start_equity or first_cash
                for d in dates:
                    val = sim_by_date.get(d)
                    if val is None:
                        # 找该日期前最近的已知权益值
                        prior = [v for dd, v in eq_pairs if dd <= d]
                        val = prior[-1] if prior else None
                    if val is not None:
                        last_val = val
                    if last_val is not None and start_equity:
                        simulation_series.append(round((last_val / start_equity - 1) * 100, 2))
                    else:
                        simulation_series.append(None)
        except Exception as e:
            logger.warning("模拟方案收益计算失败 %s: %s", code, e)
            simulation_series = [None] * len(dates)

        # ③ 大盘基准收益：按前缀映射
        market_series = self._benchmark_returns(code, dates)

        return {
            "position_id": position_id, "stock_code": code,
            "stock_name": p.stock_name, "buy_date": buy_date,
            "dates": dates, "actual": actual_series,
            "simulation": simulation_series, "market": market_series,
            "buy_hold": buy_hold_series if len(buy_hold_series) == len(dates) else actual_series,
            "actual_latest": self._last_number(actual_series),
            "simulation_latest": self._last_number(simulation_series),
            "market_latest": self._last_number(market_series),
            "buy_hold_latest": self._last_number(buy_hold_series),
            "benchmark_code": self._benchmark_code(code),
            "latest_price": latest_intraday["price"] if latest_intraday else None,
            "latest_price_date": latest_intraday["date"] if latest_intraday else None,
            "latest_price_source": "minute" if latest_intraday else "daily",
        }

    @staticmethod
    def _overlay_latest_minute(kline, code: str):
        """将最近分钟收盘价并入收益曲线最后一天，历史仍使用日线收盘。"""
        import pandas as pd
        if kline is None or kline.empty:
            return kline, None
        try:
            from StockInvestmentTool.warehouse.minute import MinuteStore

            store = MinuteStore()
            days = store.days()
            if not days:
                return kline, None
            day = days[-1]
            minute = store.read(day, code)
            if minute is None or minute.empty:
                return kline, None
            minute = minute.dropna(subset=["close"])
            if minute.empty:
                return kline, None
            latest = minute.sort_values("time").iloc[-1]
            price = float(latest["close"])
            result = kline.copy()
            result["date"] = pd.to_datetime(result["date"])
            target = pd.Timestamp(day)
            match = result["date"] == target
            if match.any():
                result.loc[match, "close"] = price
            elif target > result["date"].max():
                row = {column: None for column in result.columns}
                row.update({"date": target, "close": price})
                result = pd.concat([result, pd.DataFrame([row])], ignore_index=True)
            else:
                return result, None
            return result.sort_values("date").reset_index(drop=True), {
                "date": day, "price": round(price, 4),
                "time": str(latest.get("time") or "")[:19],
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("收益曲线分钟数据覆盖失败 %s: %s", code, exc)
            return kline, None

    @staticmethod
    def _actual_position_returns(kline, transactions: list[dict]) -> dict:
        """逐日重放真实交易流水，计算含费用的实际持仓收益率。"""
        import pandas as pd
        if kline is None or kline.empty:
            return {"dates": [], "series": []}
        first_buy = next((t for t in transactions if t.get("trans_type") == "buy"), None)
        start = str(first_buy.get("date") if first_buy else "")[:10]
        if not start:
            return {"dates": [], "series": []}
        frame = kline[pd.to_datetime(kline["date"]) >= pd.Timestamp(start)].copy()
        frame = frame.sort_values("date").reset_index(drop=True)
        shares = 0.0
        cash = 0.0
        invested = 0.0
        by_date: dict[str, list[dict]] = {}
        for txn in transactions:
            by_date.setdefault(str(txn.get("date") or "")[:10], []).append(txn)
        dates, series = [], []
        for _, row in frame.iterrows():
            day = str(row["date"])[:10]
            for txn in by_date.get(day, []):
                typ = txn.get("trans_type")
                amount = float(txn.get("amount") or 0)
                fee = float(txn.get("fee") or 0)
                qty = float(txn.get("shares") or 0)
                if typ == "buy":
                    shares += qty
                    cash -= amount + fee
                    invested += amount + fee
                elif typ in ("sell", "sell_all"):
                    shares = max(0.0, shares - qty)
                    cash += amount - fee
                elif typ == "dividend":
                    # 当前产品约定暂不把分红收益纳入累计收益曲线。
                    continue
            value = cash + shares * float(row["close"])
            dates.append(day)
            profit = value
            series.append(round(profit / invested * 100, 2) if invested else 0.0)
        return {"dates": dates, "series": series}

    @staticmethod
    def _last_number(values):
        for v in reversed(list(values)):
            if v is not None:
                return v
        return None

    def _benchmark_code(self, code: str) -> str:
        """按股票前缀匹配基准宽基指数代码。"""
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher
        try:
            norm = StockDataFetcher.normalize_code(code)
        except Exception:
            norm = code
        n = norm.lower().replace(".", "")
        if n.startswith("sh6"):
            return "sh.000001"      # 上海主板 → 上证指数
        if n.startswith("sz3"):
            return "sz.399006"      # 创业板 → 创业板指
        if n.startswith("sz"):
            return "sz.399001"      # 深圳主板 → 深证成指
        if n.startswith("sh5"):
            return "sh.000300"      # 上海 ETF → 沪深300
        return "sh.000300"

    def _benchmark_returns(self, code: str, dates: list[str]) -> list:
        """从基准指数取建仓日起的收益序列，按 dates 对齐。"""
        benchmark = self._benchmark_code(code)
        try:
            import pandas as pd
            from StockInvestmentTool.datasource.fetcher import StockDataFetcher
            with StockDataFetcher() as fetcher:
                df = fetcher.get_kline(benchmark, fields="date,close")
            if df is None or df.empty:
                return [None] * len(dates)
            df = df.sort_values("date").reset_index(drop=True)
            if not dates:
                return []
            base = None
            series = []
            idx = 0
            for d in dates:
                # 找到 >= 该日期的基准收盘
                while idx < len(df) and str(df["date"].iloc[idx])[:10] < d:
                    idx += 1
                if idx >= len(df):
                    series.append(series[-1] if series else None)
                    continue
                close = float(df["close"].iloc[idx])
                if base is None:
                    base = close or 1.0
                series.append(round((close / base - 1) * 100, 2))
            return series
        except Exception as e:
            logger.warning("大盘基准收益获取失败 %s: %s", benchmark, e)
            return [None] * len(dates)

    def _observe_one(self, item: dict) -> dict:
        """计算单只观察标的全套字段（技术面走统一 _stock_analysis 入口）。"""
        code = (item.get("code") or "").lower()
        name = item.get("name") or code
        notes = (item.get("notes") or "").strip()
        added_time = item.get("added_time") or ""

        an = self._stock_analysis(code, added_time=added_time)
        basic = an["basic"]
        ctx = an["ctx"]
        market_state = an["market_state"]

        return {
            "code": code,
            "name": name,
            "price": round(float(ctx.current_price), 2) if ctx.current_price else None,
            "market_state": market_state,
            "risk_light": _risk_light(name),
            "weak_support": basic.get("weak_support"),
            "strong_support": basic.get("strong_support"),
            "ma20": basic.get("ma20"),
            "ma60": basic.get("ma60"),
            "year_high": basic.get("year_high"),
            "trend": basic.get("trend"),
            "instruction": _open_instruction(market_state, ctx),
            "notes": notes,
            "added_time": added_time,
            "ok": True,
        }

    @staticmethod
    def _observe_fallback(item: dict, err: str) -> dict:
        code = (item.get("code") or "").lower()
        name = item.get("name") or code
        return {
            "code": code, "name": name, "price": None, "market_state": "数据缺失",
            "risk_light": "🟡待核",             "weak_support": None, "strong_support": None,
            "ma20": None, "ma60": None, "year_high": None, "trend": None,
            "instruction": f"⚠️ 数据获取失败：{err[:60]}",
            "notes": item.get("notes") or "", "ok": False,
        }

    # ── 页面二 · 持仓（作战仓）──────────────────────────

    def _position_profit_summary(self, positions: list, current_price: float | None) -> dict:
        """计算持仓卡片展示用的完整收益，不包含手续费。"""
        realized = 0.0
        invested = 0.0
        shares = sum(float(p.total_shares or 0) for p in positions)
        for position in positions:
            for txn in self.manager.storage.get_transactions(position.id):
                if txn.trans_type in ("buy", "correction") and txn.trans_type == "buy":
                    invested += float(txn.amount or 0) + float(txn.fee or 0)
                elif txn.trans_type in ("sell", "sell_all"):
                    realized += float(txn.pnl or 0)
        cost = sum(float(p.total_cost or 0) for p in positions)
        if current_price not in (None, 0):
            unrealized = float(current_price) * shares - cost
        else:
            unrealized = sum(float(p.unrealized_pnl or 0) for p in positions)
        if invested <= 0:
            invested = cost
        total = realized + unrealized
        return {
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_pnl": round(total, 2),
            "total_pnl_pct": round(total / invested * 100, 2) if invested else 0.0,
        }

    def war_room(self) -> dict:
        """持仓页：账户总览 + 每只持仓最新指令与点位。

        同一股票的多条 open 持仓按代码合并展示（股数求和/均价加权/盈亏汇总）。
        """
        summary = self.manager.get_summary()

        # 按代码分组合并 open 持仓
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher

        groups: dict[str, list] = {}
        for p in self.manager.storage.get_open_positions():
            key = StockDataFetcher.normalize_code(p.stock_code)
            groups.setdefault(key, []).append(p)

        # 持仓页的现价/浮盈不能直接使用 portfolio.db 的旧缓存；
        # 页面打开时复用已有批量实时行情，按实时价重算展示值。
        live_quotes = self._realtime_enhance(list(groups))
        live_total_market_value = 0.0
        live_total_pnl = 0.0
        positions = []
        for key, ps in groups.items():
            base = ps[0]
            latest = ps[-1]  # 最后一条的建议/点位作代表
            live_price = live_quotes.get(key.replace(".", ""), {}).get("price")
            if len(ps) > 1:
                # 合并口径: 股数/成本/市值/盈亏汇总，均价加权
                row = base.to_dict()
                row["total_shares"] = round(sum(p.total_shares for p in ps), 2)
                row["total_cost"] = round(sum(p.total_cost for p in ps), 2)
                if live_price not in (None, 0):
                    row["market_value"] = round(row["total_shares"] * float(live_price), 2)
                    row["unrealized_pnl"] = round(row["market_value"] - row["total_cost"], 2)
                else:
                    row["market_value"] = round(sum(p.market_value for p in ps), 2)
                    row["unrealized_pnl"] = round(sum(p.unrealized_pnl for p in ps), 2)
                row["unrealized_pnl_pct"] = (
                    round(row["unrealized_pnl"] / row["total_cost"] * 100, 2)
                    if row["total_cost"] else 0.0
                )
                row["avg_cost"] = (row["total_cost"] / row["total_shares"]
                                   if row["total_shares"] else 0)
                row["batch_count"] = len(ps)
                p = base  # 点位/字段用第一条，止损线合并后重算
            else:
                row = base.to_dict()
                row["batch_count"] = 1
                p = base
                if live_price not in (None, 0):
                    row["current_price"] = round(float(live_price), 4)
                    row["market_value"] = round(float(p.total_shares) * float(live_price), 2)
                    row["unrealized_pnl"] = round(row["market_value"] - float(p.total_cost), 2)
                    row["unrealized_pnl_pct"] = (
                        round(row["unrealized_pnl"] / float(p.total_cost) * 100, 2)
                        if p.total_cost else 0.0
                    )
            if live_price not in (None, 0):
                row["current_price"] = round(float(live_price), 4)
            row.update(self._position_profit_summary(ps, live_price))
            live_total_market_value += float(row.get("market_value") or 0)
            live_total_pnl += float(row.get("unrealized_pnl") or 0)
            advice = self.manager.storage.get_latest_advice(p.id)
            row["left_side"] = row["right_side"] = row["buy_more"] = None
            row["hard_cap"] = None
            if advice is not None:
                ad = advice.to_dict()
                row["advice_label"] = ADVICE_LABELS.get(ad.get("advice_type"), ad.get("advice_type", ""))
                row["advice"] = ad
                cr = ad.get("check_results") or {}
                if base.scheme_name != "minute_take_profit_v11":
                    row["left_side"] = cr.get("left_side")       # 左侧止盈: year_high/pct_of_year_high
                    row["right_side"] = cr.get("right_side")     # 右侧回撤: peak_price/drawdown_pct
                row["buy_more"] = cr.get("buy_more")         # 补仓: trigger_price/label
                if base.scheme_name != "minute_take_profit_v11":
                    yh = (cr.get("left_side") or {}).get("year_high")
                    row["hard_cap"] = round(yh * 1.05, 2) if yh else None  # 止盈硬上限
            else:
                row["advice_label"] = "—"
                row["advice"] = None
            row["fundamental"] = self._fundamental_snapshot(p.stock_code)
            # 持仓级交易记录（时间倒序，含费用/盈亏），供"操作记录"页签使用
            txns = []
            for position in ps:
                for txn in self.manager.storage.get_transactions(position.id):
                    if txn.trans_type in ("buy", "sell", "sell_all", "dividend", "correction"):
                        txns.append({**txn.to_dict(),
                                     "position_id": position.id,
                                     "stock_code": position.stock_code,
                                     "stock_name": position.stock_name,
                                     "stock_type": position.stock_type})
            txns.sort(key=lambda x: (x.get("date") or "", x.get("id") or 0), reverse=True)
            row["transactions"] = txns
            # 策略说明：优先用建仓时的方案快照，其次当前注册方案
            from StockInvestmentTool.portfolio.models import load_snapshot_scheme
            scheme_ctx = load_snapshot_scheme(base.scheme_snapshot) or None
            if scheme_ctx is None:
                try:
                    scheme_ctx = SchemeRegistry().get(base.scheme_name)
                except Exception:
                    scheme_ctx = None
            if scheme_ctx is not None:
                strategy = {
                    "scheme_name": base.scheme_name,
                    "version": getattr(scheme_ctx, "version", "1.0"),
                    "applicable_types": list(scheme_ctx.applicable_types or []),
                    "strategy_spec": getattr(scheme_ctx, "strategy_spec", {}) or {},
                    "buy_rules": [{"type": r.type} for r in getattr(scheme_ctx, "buy_rules", [])],
                    "sell_rules": [{"type": r.type} for r in getattr(scheme_ctx, "sell_rules", [])],
                }
            else:
                strategy = {"scheme_name": base.scheme_name, "version": "1.0",
                            "applicable_types": [], "strategy_spec": {},
                            "buy_rules": [], "sell_rules": []}
            row["strategy"] = strategy
            row["notify_signals"] = self._notify_signal_details(row, p, strategy)
            positions.append(row)
        # 顶部账户汇总也采用同一批实时价格，避免明细和总览口径不一致。
        if positions:
            summary["total_market_value"] = round(live_total_market_value, 2)
            summary["total_unrealized_pnl"] = round(live_total_pnl, 2)
            total_cost = float(summary.get("total_cost") or 0)
            summary["total_pnl_pct"] = round(live_total_pnl / total_cost * 100, 2) if total_cost else 0.0
        pnl_pct = summary.get("total_pnl_pct")
        return {
            "summary": summary,
            "cash": (summary.get("portfolio") or {}).get("cash_available"),
            "risk_status": _risk_status(pnl_pct),
            "positions": positions,
            "data_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "data_note": "技术指标截至最近收盘(T-1)，现价盘中实时",
            # 账户历史操作记录（按时间倒序，含所有股票/已平仓）
            "history_txns": self._history_transactions(),
        }

    @staticmethod
    def _notify_signal_details(row: dict, position, strategy: dict) -> list[dict]:
        """将当前策略检查结果转为消息通知页签可读的状态明细。"""
        from StockInvestmentTool.biz.signal_notify import ALL_SIGNALS, get_signals

        checks = (row.get("advice") or {}).get("check_results") or {}
        context = dict(checks.get("context") or {})
        if row.get("current_price") not in (None, 0):
            context["current_price"] = row["current_price"]
        enabled = get_signals(position.id)
        enabled = set(ALL_SIGNALS if enabled is None else enabled)
        buy_more = checks.get("buy_more") or {}
        rule_c = checks.get("rule_c") or {}
        hard_stop = checks.get("hard_stop") or {}
        technical = checks.get("technical_stop") or {}
        left = checks.get("left_side") or {}
        right = checks.get("right_side") or {}
        logic = checks.get("logic_stop") or {}

        rows = []
        for signal, name, group, action in [
            ("buy-support-weak", "综合弱支撑", "buy", "建议买入/加仓"),
            ("buy-support-strong", "综合强支撑", "buy", "建议买入/加仓"),
            ("buy-extreme", "极端低估锚", "buy", "建议买入/加仓"),
            ("buy-trend", "趋势跟随", "buy", "建议加仓"),
            ("stop-hard", "硬止损", "sell", "建议清仓"),
            ("stop-technical", "技术止损", "sell", "建议清仓"),
            ("take-left", "左侧固定止盈", "sell", "建议分批减仓"),
            ("take-right", "右侧移动止盈", "sell", "建议清仓"),
            ("stop-logic", "逻辑止损", "sell", "建议退出"),
        ]:
            current = "—"
            threshold = "—"
            status = "未触发"
            detail = "当前方案未产生该检查项"
            triggered = False
            if signal.startswith("buy-support"):
                target = {"buy-support-weak": "综合弱支撑", "buy-support-strong": "综合强支撑", "buy-extreme": "极端低估锚"}[signal]
                if buy_more.get("label") == target:
                    current = buy_more.get("current_price")
                    threshold = buy_more.get("trigger_price")
                    triggered = bool(buy_more.get("triggered"))
                    status = "已触发" if triggered else "未触发"
                    detail = f"当前价 {current}，触发价 {threshold}"
                else:
                    detail = f"当前待判断批次：{buy_more.get('label') or '无'}"
            elif signal == "buy-trend":
                triggered = bool(rule_c.get("triggered"))
                status = "已触发" if triggered else "未触发"
                detail = "满足趋势跟随条件" if triggered else "；".join(rule_c.get("failed") or []) or "未满足趋势条件"
            elif signal == "stop-hard":
                current, threshold = context.get("current_price"), hard_stop.get("stop_price")
                triggered = bool(hard_stop.get("triggered")); status = "已触发" if triggered else "未触发"
                detail = f"当前价 {current}，止损线 {threshold}"
            elif signal == "stop-technical":
                current, threshold = technical.get("recent_low"), technical.get("support_price") or technical.get("strong_support")
                triggered = bool(technical.get("triggered")); status = "已触发" if triggered else "未触发"
                detail = (f"最近低点 {current}，跌破支撑线 {threshold}；成交量 {technical.get('last_volume', '—')} ÷ "
                          f"前5日均量 {technical.get('reference_volume', '—')} = {technical.get('volume_ratio', '—')}倍，"
                          f"放量：{'是' if technical.get('volume_surge') else '否'}；"
                          f"规则：{'启动' if technical.get('enabled', True) else '停用'}")
            elif signal == "take-left":
                current = context.get("current_price")
                threshold = f"{left.get('zone_price_lo', '—')} ~ {left.get('zone_price_hi', '—')}"
                triggered = bool(left.get("tier", 0)); status = "已触发" if triggered else "未触发"
                detail = f"当前价 {current}，区间 {threshold}，档位 {left.get('tier', 0)}"
            elif signal == "take-right":
                current = context.get("current_price"); threshold = right.get("trigger_price") or right.get("peak_price")
                triggered = bool(right.get("triggered")); status = "已触发" if triggered else "未触发"
                detail = f"当前价 {current}，触发参考 {threshold}，峰值回撤 {right.get('drawdown_pct', '—')}%"
            elif signal == "stop-logic":
                triggered = bool(logic.get("triggered")); status = "已触发" if triggered else "未触发"
                detail = logic.get("reason") or ("逻辑已证伪" if triggered else "逻辑仍有效/未提供")
            rows.append({"signal": signal, "name": name, "group": group, "action": action,
                         "enabled": signal in enabled, "status": status, "triggered": triggered,
                         "current": current, "threshold": threshold, "detail": detail})
        return rows

    @staticmethod
    def _history_transactions() -> list[dict]:
        """全部历史操作流水（时间倒序，含所有股票及已平仓）。"""
        try:
            from StockInvestmentTool.portfolio.manager import PortfolioManager
            mgr = PortfolioManager()
            txns = []
            for p in mgr.storage.get_positions():
                for t in mgr.storage.get_transactions(p.id):
                    if t.trans_type in ("buy", "sell", "sell_all", "dividend"):
                        txns.append({**t.to_dict(),
                                     "stock_code": p.stock_code,
                                      "stock_name": p.stock_name,
                                      "stock_type": p.stock_type})
            txns.sort(key=lambda x: (x.get("date") or ""), reverse=True)
            return txns[:100]
        except Exception as e:
            logger.warning("历史流水读取失败: %s", e)
            return []

    @staticmethod
    def _fundamental_snapshot(code: str) -> Optional[dict]:
        """持仓基本面快照（营收/净利/扣非/ROE，复用财务史缓存；失败返回 None）。"""
        try:
            from StockInvestmentTool.datasource.fetcher import StockDataFetcher

            fetcher = StockDataFetcher()
            df = fetcher.get_fundamental_history(code, years=1)
            if df is None or df.empty:
                return None
            row = df.iloc[-1]

            def g(k):
                v = row.get(k)
                if v is None:
                    return None
                try:
                    return round(float(v), 2)
                except (TypeError, ValueError):
                    return None

            return {
                "revenue": g("revenue"),
                "net_profit": g("net_profit"),
                "revenue_yoy": g("revenue_yoy"),
                "roe": g("roe"),
                "date": str(row["stat_date"])[:10] if "stat_date" in row else "",
            }
        except Exception as e:
            logger.warning("基本面快照失败 %s: %s", code, e)
            return None

    # ── 大盘页（/market）───────────────────────────────

    def index_kline(self, codes: list[str], days: int = 120) -> dict:
        """多指数收盘序列（baostock）。codes: ["sh.000300","sh.000001",...]"""
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher

        result = {"dates": None, "series": {}}
        with StockDataFetcher() as fetcher:
            for code in codes:
                try:
                    df = fetcher.get_kline(code, fields="date,close")
                    if df is None or df.empty:
                        continue
                    df = df.tail(days)
                    if result["dates"] is None:
                        result["dates"] = [str(d)[:10] for d in df["date"]]
                    result["series"][code] = [round(float(x), 2) for x in df["close"]]
                except Exception as e:
                    logger.warning("指数 %s 获取失败: %s", code, e)
        return result

    def board_index_kline(self, name: str, days: int = 120, *, category: str = "ths_industry", sector_id: str = "") -> dict:
        """从已发布的 industry_daily 读取板块历史，不在页面请求时访问 AkShare。"""
        import pandas as pd

        name = str(name or "").strip()
        result = {"name": name, "dates": [], "close": [], "as_of": None,
                  "category": category, "sector_id": str(sector_id or "")}
        if not name and not sector_id:
            return result
        try:
            from StockInvestmentTool.warehouse.storage import Warehouse

            warehouse = Warehouse()
            from StockInvestmentTool.warehouse.datasets import DatasetAccess
            access = DatasetAccess(warehouse)
            if category == "csrc":
                membership = access.load_dataset("industry_membership", end_date=pd.Timestamp.now().strftime("%Y-%m-%d"), required_quality="PASS").data.copy()
                membership["snapshot_date"] = pd.to_datetime(membership["snapshot_date"], errors="coerce")
                membership = membership[membership["snapshot_date"].notna()]
                if membership.empty:
                    return {**result, "error": "暂无证监会行业成员快照"}
                membership = membership[membership["snapshot_date"] == membership["snapshot_date"].max()].copy()
                membership["code"] = membership["code"].astype(str).str.lower().str.replace(".", "", regex=False)
                relation = membership[membership["industry_code"].astype(str) == str(sector_id)]
                codes = sorted(set(relation["code"]))
                if not codes:
                    return {**result, "error": f"未找到证监会行业: {name or sector_id}"}
                end_date = pd.Timestamp.now().strftime("%Y-%m-%d")
                start_date = (pd.Timestamp.now() - pd.Timedelta(days=max(365, days * 3))).strftime("%Y-%m-%d")
                daily = access.load_dataset("stock_daily", start_date=start_date, end_date=end_date, symbols=codes, required_quality="WARNING").data.copy()
                if daily.empty:
                    return {**result, "error": "该证监会行业暂无成员日线数据"}
                daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
                daily["code"] = daily["code"].astype(str).str.lower().str.replace(".", "", regex=False)
                daily["close"] = pd.to_numeric(daily["close"], errors="coerce")
                daily = daily.dropna(subset=["date", "close"]).sort_values(["code", "date"])
                daily["return_1d"] = daily.groupby("code")["close"].pct_change()
                index = daily.dropna(subset=["return_1d"]).groupby("date")["return_1d"].mean().sort_index()
                index = ((1 + index).cumprod() * 100).tail(max(1, int(days)))
                if index.empty:
                    return {**result, "error": "该证监会行业暂无可用成员收益序列"}
                result["name"] = str(relation.iloc[0].get("industry_name") or name)
                result["dates"] = [d.strftime("%Y-%m-%d") for d in index.index]
                result["close"] = [round(float(value), 4) for value in index.values]
                result["as_of"] = result["dates"][-1]
                result["series_type"] = "证监会行业成员等权指数"
                return result
            if category != "ths_industry":
                return {**result, "error": "同花顺概念暂无板块日线数据"}
            frame = access.load_dataset("industry_daily", start_date=(pd.Timestamp.now() - pd.Timedelta(days=max(365, days * 3))).strftime("%Y-%m-%d"), end_date=pd.Timestamp.now().strftime("%Y-%m-%d"), required_quality="WARNING").data
            if frame is None or frame.empty:
                return {**result, "error": "暂无板块日线数据"}
            df = frame.copy()
            if sector_id:
                df = df[df["industry_id"].astype(str) == str(sector_id)].copy()
            else:
                df = df[df["industry_name"].astype(str) == name].copy()
            if df.empty:
                return {**result, "error": f"未找到板块: {name}"}
            result["name"] = str(df.iloc[0]["industry_name"])
            df["trading_date"] = pd.to_datetime(df["trading_date"], errors="coerce")
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df = (df.dropna(subset=["trading_date", "close"])
                    .drop_duplicates("trading_date")
                    .sort_values("trading_date")
                    .tail(max(1, int(days))))
            if df.empty:
                return {**result, "error": "该板块暂无可用日线数据"}
            result["dates"] = [d.strftime("%Y-%m-%d") for d in df["trading_date"]]
            result["close"] = [round(float(value), 2) for value in df["close"]]
            result["as_of"] = result["dates"][-1]
            return result
        except Exception as e:
            logger.warning("板块指数 %s 获取失败: %s", name, e)
            return {**result, "error": str(e)[:80]}

    def board_names(self) -> list[str]:
        """从最新 industry_daily 分区读取板块名称列表。"""
        try:
            from StockInvestmentTool.warehouse.storage import Warehouse

            warehouse = Warehouse()
            from StockInvestmentTool.warehouse.datasets import DatasetAccess
            frame = DatasetAccess(warehouse).load_dataset(
                "industry_daily", required_quality="WARNING"
            ).data
            if frame is None or frame.empty:
                return []
            return sorted({str(name) for name in frame["industry_name"].dropna() if str(name).strip()})
        except Exception as e:
            logger.warning("行业板块列表获取失败: %s", e)
            return []

    def board_overview(self, days: int = 120) -> list[dict]:
        """返回全部同花顺板块行情概览（最新价、涨跌、趋势）。"""
        import pandas as pd
        from StockInvestmentTool.warehouse.storage import Warehouse

        warehouse = Warehouse()
        from StockInvestmentTool.warehouse.datasets import DatasetAccess, DatasetAccessError
        try:
            data = DatasetAccess(warehouse).load_dataset(
                "industry_daily", required_quality="WARNING"
            ).data
        except DatasetAccessError as exc:
            logger.warning("行业板块概览 Published 数据不可用: %s", exc)
            return []
        if data is None or data.empty:
            return []
        data["trading_date"] = pd.to_datetime(data["trading_date"], errors="coerce")
        data["close"] = pd.to_numeric(data["close"], errors="coerce")
        data = data.dropna(subset=["trading_date", "close"])
        group_keys = ["industry_name"]
        if "industry_id" in data.columns:
            group_keys.insert(0, "industry_id")
        data = data.sort_values(group_keys + ["trading_date"]).drop_duplicates(
            group_keys + ["trading_date"], keep="last"
        )
        result = []
        for group_key, group in data.groupby(group_keys, sort=False):
            name = group_key[-1] if isinstance(group_key, tuple) else group_key
            group = group.tail(max(2, int(days)))
            latest = group.iloc[-1]
            previous = group.iloc[-2] if len(group) > 1 else None
            close = float(latest["close"])
            prev_close = float(previous["close"]) if previous is not None else None
            change_pct = ((close / prev_close - 1) * 100) if prev_close else None
            base = float(group.iloc[-6]["close"]) if len(group) >= 6 else float(group.iloc[0]["close"])
            change_5d = ((close / base - 1) * 100) if base else None
            trend = "上涨" if change_5d is not None and change_5d > 1 else (
                "下跌" if change_5d is not None and change_5d < -1 else "震荡"
            )
            result.append({
                "name": str(name),
                "industry_id": str(latest.get("industry_id") or ""),
                "latest_date": latest["trading_date"].strftime("%Y-%m-%d"),
                "close": round(close, 2),
                "change_pct": round(change_pct, 2) if change_pct is not None else None,
                "change_5d_pct": round(change_5d, 2) if change_5d is not None else None,
                "trend": trend,
            })
        return sorted(result, key=lambda item: (item["change_5d_pct"] is None,
                                                  -(item["change_5d_pct"] or 0)))

    def industry_rotation_overview(self, as_of: str | None = None, *, category: str = "csrc",
                                   membership_as_of: str | None = None) -> dict:
        """读取已发布行业轮动特征，供市场页展示，不访问外部接口。"""
        from datetime import date
        if category == "ths_industry":
            return self._ths_industry_rotation(as_of or date.today().isoformat(), membership_as_of=membership_as_of)
        if category == "ths_concept":
            return {"status": "no_data", "requested_as_of": as_of or date.today().isoformat(), "actual_data_as_of": None, "reason": "同花顺概念暂无已发布数据", "items": []}
        from StockInvestmentTool.warehouse.industry_features import IndustryRotationService

        requested_as_of = as_of or date.today().isoformat()
        service = IndustryRotationService()
        rows = service.decide(requested_as_of)
        context = service.last_context
        result = {
            "status": context.get("status", "no_data"),
            "requested_as_of": requested_as_of,
            "actual_data_as_of": context.get("actual_data_as_of"),
            "reason": context.get("reason"),
            "items": [],
        }
        if not rows:
            result["reason"] = result["reason"] or "暂无已发布行业轮动特征"
            return result

        state_labels = {"strong": "强势", "neutral": "中性", "weak": "弱势",
                        "insufficient_data": "数据不足"}
        leader_names = {}
        try:
            leader_names = {
                code: str(item.get("name"))
                for code, item in service.warehouse.get_instruments(
                    [str(row.get("leader_code") or "") for row in rows]
                ).items()
                if item.get("name")
            }
        except Exception as exc:
            logger.debug("读取行业龙头名称失败: %s", exc)

        def numeric(row, key, digits=4):
            value = row.get(key)
            if value is None or pd.isna(value):
                return None
            return round(float(value), digits)

        for row in rows:
            leader_code = str(row.get("leader_code") or "")
            state = str(row.get("industry_state") or "")
            display_state = "neutral" if state == "insufficient_data" and row.get("return_5d") is not None else state
            result["items"].append({
                "industry_code": str(row.get("industry_code") or ""),
                "industry_name": str(row.get("industry_name") or ""),
                "status": display_state,
                "status_label": state_labels.get(display_state, "未知"),
                "score": numeric(row, "industry_score"),
                "return_1d": numeric(row, "return_1d"),
                "return_5d": numeric(row, "return_5d"),
                "return_20d": numeric(row, "return_20d"),
                "up_ratio": numeric(row, "up_ratio"),
                "amount_ratio": numeric(row, "amount_ratio"),
                "rank_1d": numeric(row, "rank_1d", 0),
                "rank_5d": numeric(row, "rank_5d", 0),
                "rank_20d": numeric(row, "rank_20d", 0),
                "member_count": int(row.get("member_count")) if row.get("member_count") is not None and not pd.isna(row.get("member_count")) else None,
                "leader": leader_code,
                "leader_code": leader_code,
                "leader_name": leader_names.get(leader_code, leader_code),
                "reason": str(row.get("state_reason") or "") + ("；有效成员较少，状态按中性展示" if state == "insufficient_data" and row.get("return_5d") is not None else ""),
            })
        return result

    def official_rotation_workbench(self, as_of: str | None = None) -> dict:
        """Return the close-based THS industry rotation workbench snapshot."""
        from datetime import date
        from StockInvestmentTool.warehouse.datasets import DatasetAccess, DatasetAccessError
        from StockInvestmentTool.warehouse.storage import Warehouse

        requested = as_of or date.today().isoformat()
        empty = {
            "status": "no_data", "requested_as_of": requested, "actual_data_as_of": None,
            "classification": "ths_industry", "items": [], "watchlists": {}, "summary": {},
            "reason": "暂无已发布正式板块轮动状态，请先运行收盘后轮动计算任务",
        }
        try:
            result = DatasetAccess(Warehouse()).load_dataset(
                "industry_rotation_daily",
                end_date=requested, required_quality="PASS",
            )
        except DatasetAccessError:
            return empty
        frame = result.data.copy()
        if frame.empty:
            return empty
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame[frame["classification"].astype(str) == "ths_industry"].dropna(subset=["date"])
        if frame.empty:
            return empty
        actual = frame["date"].max()
        current = frame[frame["date"] == actual].copy()
        previous_dates = sorted(set(frame.loc[frame["date"] < actual, "date"]))
        previous = frame[frame["date"] == previous_dates[-1]] if previous_dates else pd.DataFrame()
        previous_counts = previous["stage"].value_counts().to_dict() if not previous.empty else {}
        leader_by_id = {}
        try:
            legacy = self._ths_industry_rotation(actual.strftime("%Y-%m-%d"), membership_as_of=actual.strftime("%Y-%m-%d"))
            leader_by_id = {str(item.get("industry_id")): item for item in legacy.get("items") or []}
        except Exception as exc:
            logger.debug("正式轮动工作台读取龙头信息失败: %s", exc)

        def num(row, key, digits=4):
            value = row.get(key)
            return None if value is None or pd.isna(value) else round(float(value), digits)

        items = []
        for _, row in current.iterrows():
            leader = leader_by_id.get(str(row.get("industry_id")), {})
            items.append({
                "industry_id": str(row.get("industry_id") or ""),
                "industry_name": str(row.get("industry_name") or ""),
                "stage": str(row.get("stage") or "DORMANT"),
                "previous_stage": str(row.get("previous_stage") or ""),
                "stage_days": int(row.get("stage_days") or 1),
                "transition": str(row.get("transition") or ""),
                "return_1d": num(row, "return_1d"), "return_5d": num(row, "return_5d"),
                "return_20d": num(row, "return_20d"), "rs_5": num(row, "rs_5"),
                "relative_return_3d": num(row, "relative_return_3d"), "relative_return_5d": num(row, "relative_return_5d"),
                "position_60": num(row, "position_60"), "amount_ratio": num(row, "amount_ratio"),
                "rank_3d": num(row, "rank_3d", 0), "rank_5d": num(row, "rank_5d", 0),
                "rank_20d": num(row, "rank_20d", 0), "rank_3d_change": num(row, "rank_3d_change", 0),
                "strength_score": num(row, "strength_score", 2),
                "rotation_score": num(row, "rotation_score", 2),
                "strength_level": num(row, "strength_level", 2),
                "rotation_heat": num(row, "rotation_heat", 2),
                "rotation_acceleration": num(row, "rotation_acceleration", 2),
                "deterioration": num(row, "deterioration", 2),
                "opportunity_score": num(row, "opportunity_score", 2),
                "transition_type": str(row.get("transition_type") or "NORMAL"),
                "rank_1d": num(row, "rank_1d", 0), "rank_3d": num(row, "rank_3d", 0),
                "rank_5d": num(row, "rank_5d", 0), "rank_3d_change": num(row, "rank_3d_change", 0),
                "rotation_rank": num(row, "rotation_rank", 0),
                "rotation_rank_1d_ago": num(row, "rotation_rank_1d_ago", 0),
                "rotation_rank_3d_ago": num(row, "rotation_rank_3d_ago", 0),
                "rotation_rank_5d_ago": num(row, "rotation_rank_5d_ago", 0),
                "strength_change": num(row, "strength_change", 2),
                "leader_code": str(leader.get("leader_code") or ""),
                "leader_name": str(leader.get("leader_name") or ""),
                "reason": str(row.get("reason") or ""), "advice": str(row.get("advice") or ""),
            })
        labels = {"DORMANT": "潜伏", "WARMING": "预热", "STARTING": "启动", "RISING": "主升", "CLIMAX": "高潮", "FADING": "退潮", "COLD": "冰点"}
        stage_order = {"DORMANT": 0, "STARTING": 1, "RISING": 2, "CLIMAX": 3, "FADING": 4, "COLD": 5}
        for item in items:
            item["stage_label"] = labels.get(item["stage"], item["stage"])
        groups = {
            "mainline": sorted([x for x in items if x["stage"] in {"WARMING", "STARTING"}], key=lambda x: (-(x.get("opportunity_score") or 0), -(x.get("rotation_acceleration") or 0), x["industry_name"]))[:5],
            "starting": sorted([x for x in items if x["stage"] == "STARTING"], key=lambda x: (-(x["rotation_score"] or 0), x["industry_name"]))[:5],
            "climax": sorted([x for x in items if x["stage"] == "CLIMAX"], key=lambda x: (-(x["strength_score"] or 0), x["industry_name"]))[:5],
            "fading": sorted([x for x in items if x["stage"] == "FADING"], key=lambda x: ((x["rotation_score"] or 0), x["industry_name"]))[:5],
        }
        counts = {stage: sum(item["stage"] == stage for item in items) for stage in labels}
        summary = {
            "mainline": counts["WARMING"] + counts["STARTING"], "starting": counts["STARTING"],
            "climax": counts["CLIMAX"], "fading": counts["FADING"],
            "mainline_delta": counts["WARMING"] + counts["STARTING"] - previous_counts.get("WARMING", 0) - previous_counts.get("STARTING", 0),
            "starting_delta": counts["STARTING"] - previous_counts.get("STARTING", 0),
            "climax_delta": counts["CLIMAX"] - previous_counts.get("CLIMAX", 0),
            "fading_delta": counts["FADING"] - previous_counts.get("FADING", 0),
            "market_state": "高低切换" if counts["STARTING"] > counts["CLIMAX"] else "主线改善" if counts["WARMING"] + counts["STARTING"] >= counts["FADING"] else "风险释放",
            "market_reason": "启动板块数量高于过热板块，优先观察低位轮动" if counts["STARTING"] > counts["CLIMAX"] else "根据正式收盘阶段分布判断，不含盘中资金流",
        }
        return {"status": "success", "requested_as_of": requested, "actual_data_as_of": actual.strftime("%Y-%m-%d"),
                "classification": "ths_industry", "items": items, "watchlists": groups, "summary": summary,
                "data_context": result.context}

    def _ths_industry_rotation(self, as_of: str, *, membership_as_of: str | None = None) -> dict:
        from StockInvestmentTool.warehouse.datasets import DatasetAccess, DatasetAccessError
        from StockInvestmentTool.warehouse.storage import Warehouse
        empty = {"status": "no_data", "requested_as_of": as_of, "actual_data_as_of": None,
                 "reason": "暂无已发布同花顺行业成员或股票日线数据", "items": []}
        try:
            access = DatasetAccess(Warehouse())
            membership_result = access.load_dataset(
                "ths_industry_membership",
                end_date=membership_as_of if membership_as_of else None,
                required_quality="PASS",
            )
            membership = membership_result.data
            membership["snapshot_date"] = pd.to_datetime(membership["snapshot_date"], errors="coerce")
            membership = membership[membership["snapshot_date"].notna()]
            if membership.empty:
                return empty
            snapshot_date = membership["snapshot_date"].max()
            membership = membership[membership["snapshot_date"] == snapshot_date].copy()
            codes = sorted(set(membership["code"].astype(str).str.lower().str.replace(".", "", regex=False)))
            start = (pd.Timestamp(as_of) - pd.Timedelta(days=75)).strftime("%Y-%m-%d")
            daily = access.load_dataset("stock_daily", start_date=start, end_date=as_of, symbols=codes, required_quality="WARNING").data
        except DatasetAccessError:
            return empty
        if membership.empty or daily.empty: return empty
        daily = daily.copy(); daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
        daily["code"] = daily["code"].astype(str).str.lower().str.replace(".", "", regex=False)
        daily["close"] = pd.to_numeric(daily["close"], errors="coerce")
        daily["amount"] = pd.to_numeric(daily.get("amount", 0), errors="coerce").fillna(0)
        daily = daily[daily["date"] <= pd.Timestamp(as_of)].dropna(subset=["date", "close"])
        common_date = daily["date"].max()
        rows = []
        for (industry_id, industry_name), relation in membership.groupby(["industry_id", "industry_name"]):
            frame = daily[daily["code"].isin(set(relation["code"].astype(str).str.lower().str.replace(".", "", regex=False)))].sort_values(["code", "date"]).copy()
            if frame.empty: continue
            for n in (1, 5, 20): frame[f"ret_{n}"] = frame.groupby("code")["close"].pct_change(n)
            last = frame[frame["date"] == common_date].copy()
            if last.empty:
                continue
            def avg(col):
                values = pd.to_numeric(last[col], errors="coerce").dropna()
                return float(values.mean()) if len(values) else None
            score = sum((avg(f"ret_{n}") or 0) * weight for n, weight in ((1, .2), (5, .4), (20, .4)))
            leader = last.sort_values(["ret_5", "amount"], ascending=False).iloc[0]
            amount = float(last["amount"].sum())
            amount_ma20 = float(frame.groupby("date")["amount"].sum().tail(20).mean())
            member_count = int(relation["code"].nunique())
            valid_count = int(last["code"].nunique())
            rows.append({"industry_id": str(industry_id), "industry_code": str(industry_id), "industry_name": str(industry_name), "status": "strong" if score > .03 else "weak" if score < -.03 else "neutral", "status_label": "强势" if score > .03 else "弱势" if score < -.03 else "中性", "score": round(score, 4), "return_1d": avg("ret_1"), "return_5d": avg("ret_5"), "return_20d": avg("ret_20"), "up_ratio": float((last["ret_1"] > 0).mean()), "amount": amount, "amount_ratio": round(amount / amount_ma20, 4) if amount_ma20 else None, "member_count": member_count, "valid_count": valid_count, "coverage": round(valid_count / member_count, 4) if member_count else 0.0, "leader_code": str(leader["code"]), "leader_name": str(leader["code"]), "reason": f"成员 {member_count} 只；共同交易日 {common_date.strftime('%Y-%m-%d')} 有效 {valid_count} 只"})
        rows.sort(key=lambda item: -(item["score"] or 0))
        for rank, row in enumerate(rows, 1): row["rank_5d"] = rank
        return {"status": "success" if rows else "no_data", "requested_as_of": as_of,
                "actual_data_as_of": common_date.strftime("%Y-%m-%d") if rows else None,
                "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
                "reason": None if rows else "同花顺行业成员与股票日线没有重叠", "items": rows}

    def board_options(self, category: str = "ths_industry") -> list[dict]:
        """Return selectable published board series for the requested category."""
        if category != "ths_industry":
            return []
        from StockInvestmentTool.warehouse.datasets import DatasetAccess, DatasetAccessError
        from StockInvestmentTool.warehouse.storage import Warehouse
        try:
            frame = DatasetAccess(Warehouse()).load_dataset(
                "industry_daily", required_quality="WARNING"
            ).data
        except DatasetAccessError:
            return []
        if frame.empty:
            return []
        return [{"sector_id": str(row.industry_id), "sector_name": str(row.industry_name),
                 "label": f"{row.industry_id}{row.industry_name}"}
                for row in frame.drop_duplicates(["industry_id", "industry_name"])
                .sort_values(["industry_id", "industry_name"]).itertuples()]

    def industry_membership_overview(self) -> dict:
        """返回最新证监会行业归属覆盖统计及各行业股票数量。"""
        import pandas as pd
        from StockInvestmentTool.warehouse.storage import Warehouse

        warehouse = Warehouse()
        files = [
            warehouse.base_dir / "industry_membership" / f"{day}.parquet"
            for day in sorted(
                path.stem for path in (warehouse.base_dir / "industry_membership").glob("*.parquet")
            )
        ]
        files = [path for path in files if path.exists()]
        if not files:
            return {"snapshot_date": None, "total_stocks": 0, "covered_stocks": 0,
                    "uncovered_stocks": 0, "coverage_pct": 0, "industries": []}
        frame = pd.read_parquet(files[-1])
        frame["code"] = frame["code"].astype(str).str.lower().str.replace(".", "", regex=False)
        frame = frame.drop_duplicates(["code", "industry_classification"])
        stock_codes = set(warehouse.all_codes())
        stock_types = warehouse.instrument_types()
        stock_codes = {code for code in stock_codes if stock_types.get(code, "stock") == "stock"}
        covered = set(frame["code"])
        covered &= stock_codes
        counts = (frame[frame["code"].isin(stock_codes)]
                  .groupby("industry_name")["code"].nunique()
                  .sort_values(ascending=False))
        return {
            "snapshot_date": str(frame["snapshot_date"].iloc[0])[:10] if len(frame) else None,
            "total_stocks": len(stock_codes),
            "covered_stocks": len(covered),
            "uncovered_stocks": len(stock_codes - covered),
            "coverage_pct": round(len(covered) / len(stock_codes) * 100, 2) if stock_codes else 0,
            "industries": [{"name": str(name), "count": int(count)} for name, count in counts.items()],
        }

    def stock_chart(self, code: str) -> dict:
        """单只股票 K 线 + advisor 点位（止损/止盈/补仓线，供持仓图叠加）。"""
        from StockInvestmentTool.portfolio.monitor import PriceMonitor
        from StockInvestmentTool.strategy.market_state import dashboard_market_state

        monitor = PriceMonitor()
        kline, dividend_anchor = monitor.fetch_context_data(code)
        ctx = self.manager.advisor.compute_context(kline, dividend_anchor)
        lines = []
        if ctx.weak_support:
            lines.append({"name": "弱支撑", "value": round(ctx.weak_support, 2), "color": "#e67e22"})
        if ctx.strong_support:
            lines.append({"name": "强支撑", "value": round(ctx.strong_support, 2), "color": "#e67e22"})
        if ctx.year_high:
            lines.append({"name": "止盈预警(前高90%)", "value": round(ctx.year_high * 0.9, 2), "color": "#28a745"})
            lines.append({"name": "止盈硬上限", "value": round(ctx.year_high * 1.05, 2), "color": "#1a73e8"})
        return {
            "code": code,
            "dates": [str(d)[:10] for d in kline["date"]],
            "close": [round(float(x), 2) for x in kline["close"]],
            "market_state": dashboard_market_state(kline),
            "lines": lines,
        }

    # ── 页面三 · 复盘底账 ───────────────────────────────

    def review_ledger(self) -> dict:
        """战后复盘：全量流水 + 统计看板（胜率/盈亏比/置信度）。"""
        txns = []
        sold = []  # 已平仓的卖出记录（用于统计）
        for p in self.manager.storage.get_positions():
            p_txns = self.manager.storage.get_transactions(p.id)
            for t in p_txns:
                txns.append({
                    **t.to_dict(),
                    "stock_code": p.stock_code, "stock_name": p.stock_name,
                    "status": p.status,
                })
                if t.trans_type in ("sell", "sell_all") and t.pnl is not None:
                    sold.append({"pnl": t.pnl, "t": t, "position": p})
        stats = self._compute_stats(sold)
        return {"txns": txns, "stats": stats, "total": len(txns)}

    @staticmethod
    def _compute_stats(sold: list[dict]) -> dict:
        """胜率 / 平均盈亏 / 盈亏比 / 持仓天数 / 置信度。"""
        n = len(sold)
        pnl = [s["pnl"] for s in sold]
        wins = [x for x in pnl if x > 0]
        losses = [x for x in pnl if x < 0]
        avg_win = sum(wins) / len(wins) if wins else None
        avg_loss = sum(losses) / len(losses) if losses else None  # 负数
        ratio = (abs(avg_win / avg_loss) if avg_win is not None and avg_loss else None)
        # 平均持仓天数: 用 position buy_date → 最后卖出日期
        days = []
        for s in sold:
            p, t = s["position"], s["t"]
            try:
                bd = datetime.strptime(p.buy_date, "%Y-%m-%d")
                sd = datetime.strptime(str(t.date)[:10], "%Y-%m-%d")
                days.append((sd - bd).days)
            except Exception:
                pass
        avg_days = round(sum(days) / len(days)) if days else None
        if n <= 5:
            conf = "🔴 极低（样本不足，谨慎参考）"
        elif n <= 19:
            conf = "🟡 中等（建议累积更多数据 ≥20 笔）"
        else:
            conf = "🟢 可信"
        return {
            "closed_trades": n,
            "win_rate": round(len(wins) / n * 100, 1) if n else None,
            "avg_win": round(avg_win, 2) if avg_win is not None else None,
            "avg_loss": round(avg_loss, 2) if avg_loss is not None else None,
            "pnl_ratio": round(ratio, 2) if ratio is not None else None,
            "avg_holding_days": avg_days,
            "confidence": conf,
        }
