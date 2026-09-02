# -*- coding: utf-8 -*-
"""持仓运行状态维护与回撤通知。

依据 docs/ACCOUNT_PORTFOLIO_AND_TRADING_DESIGN.md 与 docs/ADVICE_AND_NOTIFICATION_DESIGN.md。
- 只为 status=open 的持仓维护运行状态，覆盖写入 position_runtime_states
- 价格优先取分钟快照最新价，缺失时回退已发布日线
- 高点回撤达到阈值时生成 NotificationEvent，由 outbox_delivery 投递
- 通知只产生事件，不改变真实持仓
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from StockInvestmentTool.biz.models import now_utc

logger = logging.getLogger(__name__)

POSITION_DRAWDOWN_THRESHOLD_ENV = "POSITION_DRAWDOWN_THRESHOLD"
POSITION_DRAWDOWN_EVENT_TYPE = "POSITION_DRAWDOWN"
DEFAULT_DRAWDOWN_THRESHOLD = 0.02


@dataclass
class PositionRuntimeState:
    position_cycle_id: str
    symbol: str
    current_price: float | None
    highest_since_entry: float
    lowest_since_entry: float
    unrealized_pnl: float | None
    unrealized_pnl_pct: float | None
    max_profit_pct: float
    drawdown_from_high: float
    holding_days: int
    price_as_of: str | None
    price_source: str
    data_context: dict = field(default_factory=dict)
    updated_at: str = field(default_factory=now_utc)


def drawdown_threshold() -> float:
    """回撤阈值（0.02 = 2%）。env 可覆盖，非法值回退默认。"""
    raw = os.getenv(POSITION_DRAWDOWN_THRESHOLD_ENV, "")
    if not raw:
        return DEFAULT_DRAWDOWN_THRESHOLD
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("非法 %s=%r，使用默认 %s", POSITION_DRAWDOWN_THRESHOLD_ENV, raw, DEFAULT_DRAWDOWN_THRESHOLD)
        return DEFAULT_DRAWDOWN_THRESHOLD
    if value <= 0 or value >= 1:
        logger.warning("非法 %s=%r（需 0<x<1），使用默认 %s", POSITION_DRAWDOWN_THRESHOLD_ENV, raw, DEFAULT_DRAWDOWN_THRESHOLD)
        return DEFAULT_DRAWDOWN_THRESHOLD
    return value


def _holding_days(opened_at: str | None, as_of: str | None) -> int:
    """自然日持仓天数；缺失时返回 0。"""
    from datetime import datetime

    if not opened_at or not as_of:
        return 0
    try:
        open_dt = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
        as_of_dt = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        open_dt = open_dt.replace(tzinfo=None)
        as_of_dt = as_of_dt.replace(tzinfo=None)
    except (ValueError, TypeError):
        return 0
    delta = (as_of_dt - open_dt).days
    return max(delta, 0)


class PositionRuntimeService:
    """遍历持有中持仓，维护运行状态并触发回撤通知。"""

    def __init__(self, repo=None, price_loader: Any | None = None):
        from StockInvestmentTool.biz.repo import BusinessRepository
        self.repo = repo or BusinessRepository()
        self.price_loader = price_loader or _MinuteFirstPriceLoader()

    # ── 主入口 ────────────────────────────────────────────

    def evaluate_all(self, *, threshold: float | None = None) -> dict:
        """对全部 status=open 持仓执行一次运行状态评估。

        返回汇总：评估数量、触发通知数量、各持仓结果。
        """
        threshold = drawdown_threshold() if threshold is None else threshold
        cycles = self._open_cycles()
        summary = {"evaluated": 0, "triggered": 0, "alert_hits": 0, "positions": []}
        for cycle in cycles:
            try:
                result = self.evaluate_cycle(cycle["position_cycle_id"], threshold=threshold)
            except Exception as exc:  # noqa: BLE001
                logger.warning("持仓运行状态评估失败 %s: %s", cycle["position_cycle_id"], exc)
                continue
            summary["evaluated"] += 1
            item = result["state"]
            alert_hits = result.get("alert_hits", 0)
            item["triggered"] = result["triggered"]
            item["alert_hits"] = alert_hits
            summary["positions"].append(item)
            if result["triggered"]:
                summary["triggered"] += 1
            summary["alert_hits"] += alert_hits
        return summary

    def evaluate_cycle(self, position_cycle_id: str, *, threshold: float | None = None) -> dict:
        """评估单只持仓：取价 → 算特征 → 覆盖快照 → 触发通知。"""
        threshold = drawdown_threshold() if threshold is None else threshold
        cycle = self._get_cycle(position_cycle_id)
        if cycle is None:
            raise KeyError(f"unknown position cycle: {position_cycle_id}")

        price, price_date, price_source = self.price_loader.latest_price(cycle["symbol"])
        prior = self._prior_state(position_cycle_id)
        state = self._compute_state(cycle, price, price_date, price_source, prior)
        self._save_state(state)

        triggered = state.drawdown_from_high <= -threshold
        if triggered:
            self._emit_drawdown_event(state)
        # 用户可配置的目标价规则（后高/成本 × M%）评估
        from StockInvestmentTool.biz.position_alert import evaluate_position_alerts
        alert_hits = evaluate_position_alerts(self.repo, state.__dict__)
        return {"state": state.__dict__, "triggered": triggered, "alert_hits": alert_hits}

    # ── 数据读取 ──────────────────────────────────────────

    def _open_cycles(self) -> list[dict]:
        return self.repo.db.fetchall(
            "SELECT * FROM position_cycles WHERE status='open' ORDER BY created_at"
        )

    def _get_cycle(self, position_cycle_id: str) -> dict | None:
        return self.repo.db.fetchone(
            "SELECT * FROM position_cycles WHERE position_cycle_id=?", (position_cycle_id,)
        )

    def _prior_state(self, position_cycle_id: str) -> dict | None:
        return self.repo.db.fetchone(
            "SELECT * FROM position_runtime_states WHERE position_cycle_id=?", (position_cycle_id,)
        )

    # ── 计算 ──────────────────────────────────────────────

    def _compute_state(self, cycle: dict, price: float | None, price_date: str | None,
                       price_source: str, prior: dict | None) -> PositionRuntimeState:
        from StockInvestmentTool.biz.portfolio import PortfolioService
        pf = PortfolioService(self.repo)
        summary = pf.position_summary(cycle["position_cycle_id"])
        qty = summary["quantity"]
        avg_cost = summary["average_cost"]
        cost_basis = summary["cost_basis"]

        # 后高/低点：统一使用持仓以来日线收盘价 + 分钟收盘价口径；
        # 旧快照仍作为边界值，避免历史状态因升级而回退。
        highest = float(prior["highest_since_entry"]) if prior and prior["highest_since_entry"] is not None else price
        lowest = float(prior["lowest_since_entry"]) if prior and prior["lowest_since_entry"] is not None else price
        try:
            from StockInvestmentTool.portfolio.position_levels import calculate_position_peak
            from StockInvestmentTool.portfolio.trade_metrics import load_local_minute
            from StockInvestmentTool.portfolio.monitor import PriceMonitor
            daily = PriceMonitor().fetch_kline(cycle["symbol"], start_date=cycle["opened_at"])
            peak = calculate_position_peak(
                daily=daily, minute=load_local_minute(cycle["symbol"]),
                buy_date=cycle["opened_at"] or "", buy_price=avg_cost,
                current_price=price,
            )
            if peak.get("value") is not None:
                highest = float(peak["value"])
            if price is not None:
                lowest = min(float(lowest or price), float(price))
        except Exception as exc:
            logger.debug("统一持仓峰值计算失败，使用增量状态: %s", exc)
            if price is not None:
                if highest is None or price > highest:
                    highest = price
                if lowest is None or price < lowest:
                    lowest = price
        highest = highest if highest is not None else price
        lowest = lowest if lowest is not None else price

        unrealized = (price - avg_cost) * qty if price is not None and avg_cost is not None else None
        unrealized_pct = (price / avg_cost - 1.0) if price is not None and avg_cost else None
        max_profit_pct = (highest / avg_cost - 1.0) if highest is not None and avg_cost else 0.0
        from StockInvestmentTool.portfolio.position_levels import calculate_position_drawdown
        drawdown_value = calculate_position_drawdown(highest, price)
        drawdown = -drawdown_value if drawdown_value is not None else 0.0
        as_of = price_date or now_utc()[:10]
        holding = _holding_days(cycle["opened_at"], as_of)

        data_context = {
            "price_source": price_source,
            "threshold_drawdown": round(drawdown, 6),
            "highest_since_entry": round(highest, 4) if highest is not None else None,
            "average_cost": round(avg_cost, 4) if avg_cost is not None else None,
            "quantity": qty,
            "cost_basis": round(cost_basis, 2) if cost_basis else 0.0,
        }
        return PositionRuntimeState(
            position_cycle_id=cycle["position_cycle_id"],
            symbol=cycle["symbol"],
            current_price=price,
            highest_since_entry=highest if highest is not None else 0.0,
            lowest_since_entry=lowest if lowest is not None else 0.0,
            unrealized_pnl=unrealized,
            unrealized_pnl_pct=unrealized_pct,
            max_profit_pct=max_profit_pct,
            drawdown_from_high=drawdown,
            holding_days=holding,
            price_as_of=as_of,
            price_source=price_source,
            data_context=data_context,
            updated_at=now_utc(),
        )

    def _save_state(self, state: PositionRuntimeState) -> None:
        from StockInvestmentTool.biz.db import dumps_json
        self.repo.db.upsert("position_runtime_states", {
            "position_cycle_id": state.position_cycle_id,
            "symbol": state.symbol,
            "current_price": state.current_price,
            "highest_since_entry": state.highest_since_entry,
            "lowest_since_entry": state.lowest_since_entry,
            "unrealized_pnl": state.unrealized_pnl,
            "unrealized_pnl_pct": state.unrealized_pnl_pct,
            "max_profit_pct": state.max_profit_pct,
            "drawdown_from_high": state.drawdown_from_high,
            "holding_days": state.holding_days,
            "price_as_of": state.price_as_of,
            "price_source": state.price_source,
            "data_context_json": dumps_json(state.data_context),
            "updated_at": state.updated_at,
        }, "position_cycle_id")

    def _emit_drawdown_event(self, state: PositionRuntimeState) -> None:
        """回撤达到阈值时生成去重通知事件 + 投递记录。"""
        from StockInvestmentTool.biz.notification import NotificationService

        threshold = drawdown_threshold()
        subject = f"[持仓回撤] {state.symbol} 高点回撤 {abs(state.drawdown_from_high) * 100:.2f}%"
        text = (
            f"股票: {state.symbol}\n"
            f"事件: 持仓高点回撤\n"
            f"回撤: {abs(state.drawdown_from_high) * 100:.2f}%（阈值 {threshold * 100:.0f}%）\n"
            f"现价: {state.current_price}\n"
            f"买入后最高: {state.highest_since_entry}\n"
            f"浮动盈亏: {state.unrealized_pnl}\n"
            f"持仓天数: {state.holding_days}\n"
            f"数据时间: {state.price_as_of}（来源 {state.price_source}）\n"
        )
        payload = {
            "subject": subject,
            "text": text,
            "symbol": state.symbol,
            "position_cycle_id": state.position_cycle_id,
            "current_price": state.current_price,
            "highest_since_entry": state.highest_since_entry,
            "drawdown_from_high": round(state.drawdown_from_high, 6),
            "threshold": threshold,
            "unrealized_pnl": state.unrealized_pnl,
            "unrealized_pnl_pct": state.unrealized_pnl_pct,
            "holding_days": state.holding_days,
            "price_as_of": state.price_as_of,
            "price_source": state.price_source,
        }
        trigger_fingerprint = f"{state.position_cycle_id}|{state.price_as_of}"
        event = NotificationService(self.repo).create_event(
            event_type=POSITION_DRAWDOWN_EVENT_TYPE,
            symbol=state.symbol,
            subject_type="position_cycle",
            subject_id=state.position_cycle_id,
            priority=2,
            payload=payload,
            data_as_of=state.price_as_of or "",
            action="SELL_ALL",
            trigger_fingerprint=trigger_fingerprint,
        )
        from StockInvestmentTool.biz.notification import NotificationService as _NS
        recipient = _email_recipient()
        if recipient:
            NotificationService(self.repo).create_delivery(
                event, "email", recipient, template="position_drawdown"
            )
        logger.info("回撤通知已生成: %s drawdown=%.2f%% (as_of=%s)",
                    state.symbol, state.drawdown_from_high * 100, state.price_as_of)


def _email_recipient() -> str:
    import os
    return os.getenv("EMAIL_TO", "")


class _MinuteFirstPriceLoader:
    """最新价加载：分钟快照优先，日线兜底。

    分钟价来源 warehouse/minute/YYYY-MM-DD/minute.csv（腾讯盘中 1 分钟线，
    取当天该股票最后一笔 close）；缺失或为空时回退已发布日线最新收盘价。
    """

    def latest_price(self, symbol: str) -> tuple[float | None, str | None, str]:
        minute_price, minute_date = self._minute_price(symbol)
        if minute_price is not None:
            return minute_price, minute_date, "minute"
        return self._daily_price(symbol)

    def _minute_price(self, symbol: str) -> tuple[float | None, str | None]:
        try:
            from datetime import datetime
            from StockInvestmentTool.warehouse.minute import MinuteStore, normalize_minute_code
            from StockInvestmentTool.config import Config

            store = MinuteStore()
            # 不硬取"今天"：凌晨/非交易时段当天无分钟数据，应取最近一个
            # 已有分钟快照的交易日（如上一个交易日），否则会误判为无价格。
            days = store.days()
            today = datetime.now().strftime("%Y-%m-%d")
            day = None
            if days:
                past = [d for d in days if d <= today]
                day = past[-1] if past else days[-1]
            if day is None:
                return None, None
            frame = store.read(day, normalize_minute_code(symbol))
            if frame is None or frame.empty:
                return None, None
            frame = frame.dropna(subset=["close"])
            if frame.empty:
                return None, None
            last = frame.sort_values("time").iloc[-1]
            return float(last["close"]), day
        except Exception as exc:  # noqa: BLE001
            logger.warning("分钟价读取失败 %s: %s", symbol, exc)
            return None, None

    def _daily_price(self, symbol: str) -> tuple[float | None, str | None, str]:
        try:
            from datetime import datetime, timedelta
            from StockInvestmentTool.biz.data_access import load_market_data
            from StockInvestmentTool.warehouse.storage import Warehouse

            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            result = load_market_data(
                Warehouse(), start_date=start, end_date=end, symbols=[symbol],
            )
            df = result.data
            if df is None or df.empty:
                return None, None, "daily_fallback_missing"
            df = df.sort_values("date")
            last = df.iloc[-1]
            price = float(last["close"]) if last["close"] is not None else None
            return price, str(df["date"].iloc[-1]), "daily"
        except Exception as exc:  # noqa: BLE001
            logger.warning("日线兜底读取失败 %s: %s", symbol, exc)
            return None, None, "daily_fallback_missing"
