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
        for w in self.manager.get_watchlist():
            items.append({"code": w.stock_code, "name": w.stock_name, "notes": w.notes or ""})
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
        """每次打开页面用腾讯实时报价刷新现价/涨跌幅（一次批量请求，轻量）。

        技术指标（市场状态/支撑/指令）基于日K按日缓存，不随盘中变化；
        现价则是实时的。腾讯失败时降级保留原价。
        """
        if not rows:
            return rows
        try:
            from StockInvestmentTool.screener.sources import tencent_quotes

            codes = [r["code"].replace(".", "").lower() for r in rows]
            quotes = tencent_quotes(codes)
            by = {q["code"]: q for _, q in quotes.iterrows()}
            updated = 0
            for r in rows:
                q = by.get(r["code"].replace(".", "").lower())
                if q is not None and q.get("price") not in (None, 0):
                    r["price"] = round(float(q["price"]), 2)
                    if q.get("change_pct") is not None:
                        r["change_pct"] = round(float(q["change_pct"]), 2)
                    updated += 1
            logger.info("观察池现价已实时刷新 %d/%d 只", updated, len(rows))
        except Exception as e:
            logger.warning("观察池现价实时刷新失败(降级用缓存价): %s", e)
        return rows

    def _flow_candidates(self, top_n: int) -> list[dict]:
        """从 fundflow 持续流入榜取候选（只取代码/名称/净额，不阻塞主流程）。"""
        if top_n <= 0:
            return []
        try:
            from StockInvestmentTool.fundflow import analysis, sources

            stk_now = sources.fetch_stock("now")
            stk_3d = sources.fetch_stock("3d")
            res = analysis.stock_analysis(stk_now, stk_3d, top=top_n)
            out = []
            for _, r in res["持续流入榜"].head(top_n).iterrows():
                out.append({"code": r["code"], "name": r["name"],
                            "notes": f"资金持续流入(3日{r.get('net_days'):.1f}亿)"})
            return out
        except Exception as e:
            logger.warning("资金流候选获取失败: %s", e)
            return []

    def _observe_one(self, item: dict) -> dict:
        """计算单只观察标的全套字段。"""
        from StockInvestmentTool.portfolio.monitor import PriceMonitor
        from StockInvestmentTool.strategy.market_state import dashboard_market_state

        code = (item.get("code") or "").lower()
        name = item.get("name") or code
        notes = (item.get("notes") or "").strip()

        monitor = PriceMonitor()
        kline, dividend_anchor = monitor.fetch_context_data(code)
        ctx = self.manager.advisor.compute_context(kline, dividend_anchor)
        market_state = dashboard_market_state(kline)

        return {
            "code": code,
            "name": name,
            "price": round(float(ctx.current_price), 2) if ctx.current_price else None,
            "market_state": market_state,
            "risk_light": _risk_light(name),
            "weak_support": round(ctx.weak_support, 2) if ctx.weak_support else None,
            "strong_support": round(ctx.strong_support, 2) if ctx.strong_support else None,
            "ma_20": round(ctx.ma_20, 2) if ctx.ma_20 else None,
            "instruction": _open_instruction(market_state, ctx),
            "notes": notes,
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
        from StockInvestmentTool.data.fetcher import StockDataFetcher

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
        }

    @staticmethod
    def _fundamental_snapshot(code: str) -> Optional[dict]:
        """持仓基本面快照（营收/净利/扣非/ROE，复用财务史缓存；失败返回 None）。"""
        try:
            from StockInvestmentTool.data.fetcher import StockDataFetcher

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
        from StockInvestmentTool.data.fetcher import StockDataFetcher

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
