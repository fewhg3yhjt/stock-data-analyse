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
from datetime import datetime
from typing import Optional

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
        return f"⏳回踩MA20（{ctx.ma_20:.2f}）建仓"
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

    def observe_pool(self, max_candidates: int = 15, use_cache: bool = True) -> list[dict]:
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
                return self._refresh_prices(rows)   # 现价实时刷新，技术指标用缓存
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
        return self._refresh_prices(rows)

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
                          "volume", "high", "low", "open", "prev_close"):
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

    def stock_dual_view(self, code: str, days: int = 120) -> dict:
        """单只股票的「天周期历史 + 盘中快照」双视图数据（后端打通）。

        框1 天周期历史: warehouse daily 分区（OHLCV/amount/turn/PE/PB）
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
            conn = w._conn()
            try:
                row = conn.execute(
                    "SELECT name FROM instruments WHERE code=?", (code_nodot,)
                ).fetchone()
                if row and row[0]:
                    result["name"] = row[0]
            finally:
                conn.close()
        except Exception as e:
            logger.warning("标的名称读取失败 %s: %s", code, e)

        # 框1：天周期历史（从 warehouse 读，避免重复拉网络）
        try:
            w = Warehouse()
            history = []
            for ym in w.available_months("daily"):
                df = w.read_daily(ym)
                if df is None or df.empty or "code" not in df.columns:
                    continue
                sub = df[df["code"] == code_nodot].tail(days)
                if len(sub):
                    history.append(sub)
            if history:
                import pandas as pd
                hdf = pd.concat(history, ignore_index=True).sort_values("date").tail(days)
                result["daily_history"] = {
                    "dates": [str(d)[:10] for d in hdf["date"]],
                    "closes": [round(float(x), 2) if x == x else None for x in hdf["close"]],
                    "volumes": [round(float(x), 0) if x == x else None for x in hdf["volume"]],
                    "amounts": [round(float(x), 2) if x == x else None for x in hdf.get("amount", pd.Series([None]*len(hdf)))],
                    "turns": [round(float(x), 2) if x == x else None for x in hdf.get("turn", pd.Series([None]*len(hdf)))],
                    "pe": [round(float(x), 2) if x == x else None for x in hdf.get("peTTM", pd.Series([None]*len(hdf)))],
                    "pb": [round(float(x), 2) if x == x else None for x in hdf.get("pbMRQ", pd.Series([None]*len(hdf)))],
                }
        except Exception as e:
            logger.warning("天周期历史读取失败 %s: %s", code, e)

        # 框2：盘中快照（当日最新）
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            w = Warehouse()
            snaps = w.online_snapshots(today)
            if snaps:
                import pandas as pd
                latest = pd.read_csv(snaps[-1], encoding="utf-8-sig")
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
        except Exception as e:
            logger.warning("盘中快照读取失败 %s: %s", code, e)

        return result

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
        from StockInvestmentTool.warehouse.storage import Warehouse
        from StockInvestmentTool.indicators.engine import IndicatorRegistry

        norm = StockDataFetcher.normalize_code(code)
        code_nodot = norm.replace(".", "")
        if metrics is None:
            metrics = ["MA20", "MA60"]

        # 读 warehouse 日线
        w = Warehouse()
        parts = []
        for ym in w.available_months("daily"):
            df = w.read_daily(ym)
            if df is None or df.empty or "code" not in df.columns:
                continue
            sub = df[df["code"] == code_nodot]
            if len(sub):
                parts.append(sub)
        if not parts:
            return {"code": code_nodot, "dates": [], "closes": [],
                    "metrics": [], "ret_cost": [], "ret_price": [],
                    "avg_cost": cost_price}
        import pandas as pd
        kline = pd.concat(parts, ignore_index=True).sort_values("date").tail(days * 2)

        # 计算指标（指标体系，日线）
        reg = IndicatorRegistry()
        ind_series = reg.compute(kline, metrics)

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
                       ma_20, ma_60, trend, year_high}}
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
            "ma_20": round(ctx.ma_20, 2) if ctx.ma_20 else None,
            "ma_60": round(ctx.ma_60, 2) if ctx.ma_60 else None,
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
        })

        dual = self.stock_dual_view(norm)

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
            "ma_20": basic.get("ma_20"),
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
            "risk_light": "🟡待核", "weak_support": None, "strong_support": None,
            "ma_20": None, "instruction": f"⚠️ 数据获取失败：{err[:60]}",
            "notes": item.get("notes") or "", "ok": False,
        }

    # ── 页面二 · 持仓（作战仓）──────────────────────────

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

        positions = []
        for key, ps in groups.items():
            base = ps[0]
            latest = ps[-1]  # 最后一条的建议/点位作代表
            if len(ps) > 1:
                # 合并口径: 股数/成本/市值/盈亏汇总，均价加权
                row = base.to_dict()
                row["total_shares"] = round(sum(p.total_shares for p in ps), 2)
                row["total_cost"] = round(sum(p.total_cost for p in ps), 2)
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
            advice = self.manager.storage.get_latest_advice(p.id)
            row["left_side"] = row["right_side"] = row["buy_more"] = None
            row["hard_cap"] = None
            if advice is not None:
                ad = advice.to_dict()
                row["advice_label"] = ADVICE_LABELS.get(ad.get("advice_type"), ad.get("advice_type", ""))
                row["advice"] = ad
                cr = ad.get("check_results") or {}
                row["left_side"] = cr.get("left_side")       # 左侧止盈: year_high/pct_of_year_high
                row["right_side"] = cr.get("right_side")     # 右侧回撤: peak_price/drawdown_pct
                row["buy_more"] = cr.get("buy_more")         # 补仓: trigger_price/label
                yh = (cr.get("left_side") or {}).get("year_high")
                row["hard_cap"] = round(yh * 1.05, 2) if yh else None  # 止盈硬上限
            else:
                row["advice_label"] = "—"
                row["advice"] = None
            row["fundamental"] = self._fundamental_snapshot(p.stock_code)
            positions.append(row)
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
                                     "stock_name": p.stock_name})
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

    def board_index_kline(self, name: str, days: int = 120) -> dict:
        """同花顺行业板块指数历史（akshare）。name: 板块名如 '半导体'"""
        try:
            import akshare as ak
            import pandas as pd

            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now().replace(year=datetime.now().year - 1)).strftime("%Y%m%d")
            df = ak.stock_board_industry_index_ths(symbol=name, start_date=start, end_date=end)
            df = df.tail(days)
            return {
                "name": name,
                "dates": [str(d)[:10] for d in df["date"]],
                "close": [round(float(x), 2) for x in df["close"]],
            }
        except Exception as e:
            logger.warning("板块指数 %s 获取失败: %s", name, e)
            return {"name": name, "dates": [], "close": [], "error": str(e)[:80]}

    def board_names(self) -> list[str]:
        """同花顺行业板块名列表（大盘页下拉用）。"""
        try:
            import akshare as ak

            df = ak.stock_board_industry_name_ths()
            return list(df["name"])
        except Exception as e:
            logger.warning("行业板块列表获取失败: %s", e)
            return []

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
