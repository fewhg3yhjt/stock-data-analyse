"""组合管理器 — 组合级别的操作控制器

职责:
  - 建仓 / 记录交易 / 平仓 / 编辑纠错 / 删除
  - 刷新价格 + 重算指标 + 生成建议
  - 组合总览（市值/盈亏/现金）
  - 状态机维护 (accumulating → holding → left_side → right_side → closed)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from StockInvestmentTool.core.registry import SchemeRegistry
from StockInvestmentTool.core.scheme import SchemeConfig
from StockInvestmentTool.portfolio.models import (
    ActionAdvice,
    Position,
    Simulation,
    Transaction,
    Portfolio,
    WatchlistItem,
    PHASE_ACCUMULATING,
    PHASE_HOLDING,
    PHASE_LEFT_SIDE,
    PHASE_RIGHT_SIDE,
    PHASE_CLOSED,
    STATUS_OPEN,
    STATUS_CLOSED,
    TXN_BUY,
    TXN_SELL,
    TXN_SELL_ALL,
    TXN_DIVIDEND,
    TXN_CORRECTION,
    snapshot_scheme,
    _now,
)
from StockInvestmentTool.portfolio.advisor import PostPurchaseAdvisor
from StockInvestmentTool.portfolio.monitor import PriceMonitor
from StockInvestmentTool.portfolio.storage import PortfolioStorage
from StockInvestmentTool.strategy.position_state import (
    PositionStateMachine, EVENT_BOUGHT, EVENT_LEFT_TP, EVENT_STOP, EVENT_BREAKOUT,
)

logger = logging.getLogger(__name__)


class PortfolioManager:
    """组合管理器"""

    def __init__(self, storage: Optional[PortfolioStorage] = None,
                 registry: Optional[SchemeRegistry] = None):
        self.storage = storage or PortfolioStorage()
        self.registry = registry or SchemeRegistry()
        self.advisor = PostPurchaseAdvisor(self.storage, self.registry)
        self.monitor = PriceMonitor()
        self.state_machine = PositionStateMachine()

    def _phase_after(self, current: str, event: str) -> str:
        if self.state_machine.can(current, event):
            return self.state_machine.transition(current, event)
        if event == "closed" and self.state_machine.can(current, EVENT_STOP):
            return self.state_machine.transition(current, EVENT_STOP)
        return current

    # ══════════════════════════════════════════════════
    # 建仓
    # ══════════════════════════════════════════════════

    def add_position(self, stock_code: str, stock_name: str,
                     shares: float, cost: float, buy_date: str,
                     scheme_name: str = "default_value",
                     stock_type: str = "B",
                     notes: str = "",
                     portfolio_id: int = 1) -> Position:
        """新建持仓

        流程: 校验方案 → 拉数据 → 初始化指标 → 建 Position → 记 buy 交易 → 生成初始建议
        """
        if shares <= 0 or cost <= 0:
            raise ValueError("份额和成本必须为正数")

        # 源头去重：同一股票只允许一条 open 持仓，加仓走交易流水
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher

        code_norm = StockDataFetcher.normalize_code(stock_code)
        for p in self.storage.get_open_positions():
            if StockDataFetcher.normalize_code(p.stock_code) == code_norm:
                raise ValueError(
                    f"{stock_name} 已有持仓(#{p.id})，加仓请走交易流水"
                )

        scheme = self.registry.get(scheme_name)
        code = stock_code

        # 建仓主流程只读取本地/统一行情层的 K 线；股息估值锚点会访问
        # 外部多年数据，不能阻塞真实持仓写入。
        kline = self.monitor.fetch_kline(code)
        dividend_anchor = None
        last = kline.iloc[-1] if len(kline) else None
        current_price = float(last["close"]) if last is not None else cost
        from StockInvestmentTool.portfolio.fees import calculate_trade_fees
        entry_fee = calculate_trade_fees(
            shares * cost, stock_type=stock_type, direction="buy"
        ).total

        position = Position(
            portfolio_id=portfolio_id,
            stock_code=code,
            stock_name=stock_name,
            stock_type=stock_type,
            scheme_name=scheme_name,
            scheme_snapshot=snapshot_scheme(scheme),
            total_shares=shares,
            avg_cost=(shares * cost + entry_fee) / shares,
            total_cost=shares * cost + entry_fee,
            current_price=current_price,
            peak_price=current_price,
            position_phase=PHASE_ACCUMULATING,
            buy_stage=1,                       # 首批已买
            left_tier_sold=0,
            stop_loss_price=self._compute_stop_loss(scheme, stock_type, cost),
            buy_date=buy_date,
            last_operated_date=buy_date,
            status=STATUS_OPEN,
            notes=notes,
        )
        # 持仓、初始交易和现金扣减必须同一事务提交。
        self.storage.atomic_position_transaction(position, Transaction(
            position_id=position.id, trans_type=TXN_BUY,
            date=buy_date, price=cost, shares=shares,
            amount=shares * cost, reason="建仓",
            ), -(shares * cost + entry_fee))

        # 初始建议
        self._save_advice(position, kline, dividend_anchor)

        # 自动加入自选（观察池数据源；去重，已存在则跳过）
        try:
            self._ensure_watchlist(code, stock_name)
        except Exception as e:
            logger.debug("自动加自选失败 %s: %s", code, e)

        logger.info("建仓成功: %s (%s) %s股 @%.2f，方案 %s",
                    stock_name, code, shares, cost, scheme_name)
        return position

    def _ensure_watchlist(self, stock_code: str, stock_name: str,
                          added_time: str = "", source: str = "holding") -> Optional[WatchlistItem]:
        """确保股票在自选列表（去重）。

        Args:
            source: 来源（holding 持仓自动同步 / manual 手动 / strategy 策略选入）
        """
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher

        code_norm = StockDataFetcher.normalize_code(stock_code)
        for w in self.storage.get_watchlist():
            if StockDataFetcher.normalize_code(w.stock_code) == code_norm:
                return w
        return self.storage.add_watchlist(
            WatchlistItem(stock_code=stock_code, stock_name=stock_name,
                          added_time=added_time or datetime.now().strftime("%Y-%m-%d"),
                          source=source)
        )

    def sync_holdings_to_watchlist(self) -> int:
        """把所有 open 持仓同步进自选（幂等去重，source=holding），返回新增数。

        用于回填功能上线前的既有持仓，以及持仓页访问时的兜底。
        """
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher

        existing = {StockDataFetcher.normalize_code(w.stock_code)
                    for w in self.storage.get_watchlist()}
        added = 0
        for p in self.storage.get_open_positions():
            norm = StockDataFetcher.normalize_code(p.stock_code)
            if norm not in existing:
                self.storage.add_watchlist(
                    WatchlistItem(stock_code=p.stock_code, stock_name=p.stock_name,
                                  source="holding")
                )
                existing.add(norm)
                added += 1
        return added

    def sync_strategy_candidates(self, top_n: int = 15) -> int:
        """把观察策略选中的股票（资金流持续流入）同步进观察池（source=strategy）。

        返回新增数。幂等：已有股票跳过；策略候选每日刷新（超出的旧候选不主动删除，
        保留作为历史观察记录）。
        """
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher

        existing = {StockDataFetcher.normalize_code(w.stock_code)
                    for w in self.storage.get_watchlist()}
        added = 0
        try:
            from StockInvestmentTool.portfolio.dashboard import DashboardService
            # 复用看板观察池的资金流候选逻辑
            candidates = DashboardService(self).observe_pool(max_candidates=top_n,
                                                             use_cache=True)
            for r in candidates:
                code = (r.get("code") or "").strip().lower()
                if not code:
                    continue
                norm = StockDataFetcher.normalize_code(code)
                if norm in existing:
                    continue
                name = r.get("name") or code
                notes = r.get("notes") or "资金流策略选入"
                self.storage.add_watchlist(
                    WatchlistItem(stock_code=code, stock_name=name,
                                  notes=notes, source="strategy")
                )
                existing.add(norm)
                added += 1
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("策略候选同步失败: %s", e)
        return added

    # ══════════════════════════════════════════════════
    # 交易记录
    # ══════════════════════════════════════════════════

    def record_transaction(self, position_id: int, trans_type: str,
                           price: float, shares: float = 0,
                           date: Optional[str] = None,
                           fee: float = 0.0, reason: str = "") -> Transaction:
        """记录一笔交易并更新持仓状态

        支持类型: buy / sell / sell_all / dividend / correction
        """
        position = self.storage.get_position(position_id)
        if position is None:
            raise ValueError(f"持仓不存在: {position_id}")
        if position.status == STATUS_CLOSED and trans_type != TXN_CORRECTION:
            raise ValueError("持仓已平仓，无法记录交易（纠错除外）")

        date = date or datetime.now().strftime("%Y-%m-%d")
        amount = price * shares

        if trans_type in (TXN_BUY, TXN_SELL, TXN_SELL_ALL):
            from StockInvestmentTool.portfolio.fees import calculate_trade_fees
            fee = calculate_trade_fees(
                amount, stock_type=position.stock_type,
                direction="sell" if trans_type in (TXN_SELL, TXN_SELL_ALL) else "buy",
            ).total
        else:
            fee = float(fee or 0)

        txn_pnl = 0.0
        if trans_type == TXN_BUY:
            position = self._apply_buy(position, price, shares, fee)
            # 加仓自动同步到自选（观察池数据源；幂等去重）
            try:
                self._ensure_watchlist(position.stock_code, position.stock_name)
            except Exception as e:
                logger.debug("加仓自动同步自选失败 %s: %s", position.stock_code, e)
        elif trans_type in (TXN_SELL, TXN_SELL_ALL):
            position, txn_pnl = self._apply_sell(position, trans_type, price, shares, fee)
        elif trans_type == TXN_DIVIDEND:
            position = self._apply_dividend(position, price, shares, fee)
        elif trans_type == TXN_CORRECTION:
            position = self._apply_correction(position, price, shares, reason)
        else:
            raise ValueError(f"不支持的交易类型: {trans_type}")

        position.last_operated_date = date
        txn = Transaction(
            position_id=position.id, trans_type=trans_type, date=date,
            price=price, shares=shares, amount=amount, fee=fee,
            pnl=txn_pnl, reason=reason,
        )
        self.storage.atomic_update_transaction(
            position, txn,
            -(price * shares + fee) if trans_type == TXN_BUY
            else (price * shares - fee if trans_type in (TXN_SELL, TXN_SELL_ALL, TXN_DIVIDEND) else 0.0),
        )
        logger.info("记录交易: 持仓#%d %s %s股 @%.2f", position_id, trans_type, shares, price)
        # 交易后自动重新分析，更新现价/均价/点位/建议（失败不阻塞交易）
        try:
            self.refresh_position(position_id)
        except Exception as e:
            logger.warning("交易后自动分析失败(持仓#%d): %s", position_id, e)
        return txn

    def _apply_buy(self, p: Position, price: float, shares: float, fee: float) -> Position:
        """买入: 份额增加, 均价加权, 批次++, 扣现金"""
        old_cost = p.avg_cost * p.total_shares
        buy_cost = price * shares + fee
        new_total = p.total_shares + shares
        if new_total <= 0:
            raise ValueError("买入后份额必须为正")
        p.avg_cost = (old_cost + buy_cost) / new_total
        p.total_shares = new_total
        p.total_cost += buy_cost
        p.buy_stage = min(p.buy_stage + 1, 3)
        if p.buy_stage >= 3:
            p.position_phase = self._phase_after(p.position_phase, EVENT_BOUGHT)
        else:
            p.position_phase = PHASE_ACCUMULATING
        # 重算止损线
        scheme = self._load_snapshot(p)
        p.stop_loss_price = self._compute_stop_loss(scheme, p.stock_type, p.avg_cost)
        return p

    def _apply_sell(self, p: Position, trans_type: str, price: float,
                    shares: float, fee: float) -> tuple[Position, float]:
        """卖出: 份额减少, 记 pnl, 更新阶段

        Returns:
            (更新后的持仓, 已实现盈亏)
        """
        if shares <= 0 or shares > p.total_shares:
            raise ValueError(f"卖出份额无效: {shares}（当前持有 {p.total_shares}）")

        # 已实现盈亏
        pnl = (price - p.avg_cost) * shares - fee
        p.total_cost = max(0.0, p.total_cost - p.avg_cost * shares)
        p.total_shares -= shares
        if p.total_shares < 1e-9:
            p.total_shares = 0.0

        if p.total_shares <= 0:
            # 全部清仓
            p.status = STATUS_CLOSED
            p.position_phase = self._phase_after(p.position_phase, EVENT_STOP)
        else:
            # 部分卖出 → 视作左侧止盈（进入 left_side 阶段）
            if p.position_phase in (PHASE_ACCUMULATING, PHASE_HOLDING):
                p.position_phase = self._phase_after(p.position_phase, EVENT_LEFT_TP)
                p.left_tier_sold = min(p.left_tier_sold + 1, 2)
            # 已处于 left_side/right_side 则保持

        return p, pnl

    def _apply_dividend(self, p: Position, price: float, shares: float, fee: float) -> Position:
        """分红: 现金增加, 份额/成本不变"""
        return p

    def _apply_correction(self, p: Position, price: float, shares: float, reason: str) -> Position:
        """纠错: 直接覆盖份额与均价（用于数据修正）"""
        if shares < 0:
            raise ValueError("份额不能为负")
        p.total_shares = shares
        if price > 0:
            p.avg_cost = price
            p.total_cost = shares * price
            scheme = self._load_snapshot(p)
            p.stop_loss_price = self._compute_stop_loss(scheme, p.stock_type, p.avg_cost)
        if shares <= 0:
            p.status = STATUS_CLOSED
            p.position_phase = self._phase_after(p.position_phase, EVENT_STOP)
        return p

    # ══════════════════════════════════════════════════
    # 平仓 / 删除 / 编辑
    # ══════════════════════════════════════════════════

    def close_position(self, position_id: int, price: float,
                       date: Optional[str] = None, reason: str = "平仓") -> Transaction:
        """一键平仓（= 卖出全部持仓）"""
        p = self.storage.get_position(position_id)
        if p is None:
            raise ValueError(f"持仓不存在: {position_id}")
        if p.total_shares <= 0:
            raise ValueError("持仓份额为0，无需平仓")
        return self.record_transaction(
            position_id, TXN_SELL_ALL, price, p.total_shares, date, reason=reason
        )

    def edit_position(self, position_id: int, shares: Optional[float] = None,
                      avg_cost: Optional[float] = None,
                      notes: Optional[str] = None) -> Position:
        """编辑/纠错持仓（直接改数值，记录 correction 交易）"""
        p = self.storage.get_position(position_id)
        if p is None:
            raise ValueError(f"持仓不存在: {position_id}")

        old_shares, old_cost = p.total_shares, p.avg_cost
        if shares is not None:
            p.total_shares = shares
        if avg_cost is not None:
            p.avg_cost = avg_cost
            p.total_cost = p.total_shares * avg_cost
            scheme = self._load_snapshot(p)
            p.stop_loss_price = self._compute_stop_loss(scheme, p.stock_type, p.avg_cost)
        if notes is not None:
            p.notes = notes
        if p.total_shares <= 0:
            p.status = STATUS_CLOSED
            p.position_phase = PHASE_CLOSED

        self.storage.update_position(p)
        self.storage.add_transaction(Transaction(
            position_id=p.id, trans_type=TXN_CORRECTION,
            date=datetime.now().strftime("%Y-%m-%d"), price=avg_cost or old_cost,
            shares=shares if shares is not None else old_shares,
            amount=0, reason=f"纠错: 份额 {old_shares}→{shares}, 均价 {old_cost}→{avg_cost}",
        ))
        return p

    def delete_position(self, position_id: int) -> dict:
        """删除持仓（破坏性）：open 持仓还原占用资金，级联删交易/建议。

        返回: {status, restored_cash, stock_name}
        """
        p = self.storage.get_position(position_id)
        if p is None:
            raise ValueError(f"持仓不存在: {position_id}")
        restored = 0.0
        # open 持仓: 把累计成本还原回可用资金（流水还原）
        if p.status == STATUS_OPEN and p.total_cost > 0:
            restored = round(p.total_cost, 2)
            self.storage.adjust_cash(restored)
        self.storage.delete_position(position_id)
        logger.info("删除持仓 #%d %s，还原资金 %.2f", position_id, p.stock_name, restored)
        return {"status": "success", "restored_cash": restored,
                "stock_name": p.stock_name}

    # ══════════════════════════════════════════════════
    # 刷新 + 建议
    # ══════════════════════════════════════════════════

    def refresh_all(self) -> list[dict]:
        """刷新所有持仓的最新价 + 重跑建议

        Returns:
            [{position_id, stock_code, current_price, pnl_pct, advice_type, urgency}]
        """
        results = []
        for p in self.storage.get_open_positions():
            results.append(self.refresh_position(p.id))
        return results

    def refresh_position(self, position_id: int) -> dict:
        """刷新单个持仓"""
        p = self.storage.get_position(position_id)
        if p is None:
            raise ValueError(f"持仓不存在: {position_id}")
        if p.status != STATUS_OPEN:
            return {"position_id": position_id, "note": "已平仓"}

        kline, dividend_anchor = self.monitor.fetch_context_data(p.stock_code)
        last = kline.iloc[-1] if len(kline) else None

        if last is not None:
            p.current_price = float(last["close"])
            high = float(last["high"])
            if high > p.peak_price:
                p.peak_price = high

        self.storage.update_position(p)
        advice = self._save_advice(p, kline, dividend_anchor)

        return {
            "position_id": p.id,
            "stock_code": p.stock_code,
            "stock_name": p.stock_name,
            "current_price": p.current_price,
            "peak_price": p.peak_price,
            "pnl_pct": p.unrealized_pnl_pct,
            "advice_type": advice.advice_type,
            "urgency": advice.urgency,
            "reason": advice.reason,
        }

    def analyze_position(self, position_id: int) -> ActionAdvice:
        """用已有 kline 重新分析（不拉新数据）"""
        p = self.storage.get_position(position_id)
        if p is None:
            raise ValueError(f"持仓不存在: {position_id}")
        kline, dividend_anchor = self.monitor.fetch_context_data(p.stock_code)
        return self._save_advice(p, kline, dividend_anchor)

    def change_position_scheme(self, position_id: int, scheme_name: str) -> dict:
        """切换持仓方案并重新生成止盈止损位（止损线按新方案重算 + 重新分析）。"""
        p = self.storage.get_position(position_id)
        if p is None:
            raise ValueError(f"持仓不存在: {position_id}")
        if p.status != STATUS_OPEN:
            raise ValueError("已平仓持仓无法切换方案")
        scheme = self.registry.get(scheme_name)  # 校验方案存在
        p.scheme_name = scheme_name
        p.scheme_snapshot = snapshot_scheme(scheme)
        # 止损线按新方案重算
        p.stop_loss_price = self._compute_stop_loss(scheme, p.stock_type, p.avg_cost)
        self.storage.update_position(p)
        return self.refresh_position(position_id)

    def _save_advice(self, position: Position, kline, dividend_anchor) -> ActionAdvice:
        """生成并保存建议"""
        advice = self.advisor.analyze_position(position, kline, dividend_anchor)
        self.storage.save_advice(advice)
        # 持久化 phase 变化（advisor 可能更新了 position.position_phase）
        self.storage.update_position(position)
        try:
            self._emit_signal_notifications(position, advice)
        except Exception as e:
            logger.warning("持仓信号通知失败 %s: %s", position.stock_code, e)
        return advice

    def _emit_signal_notifications(self, position: Position, advice: ActionAdvice) -> None:
        """按勾选的策略信号发送邮件通知（去重：同持仓同信号同日一次）。"""
        import os
        from StockInvestmentTool.biz.notification import NotificationService
        from StockInvestmentTool.biz.signal_notify import ALL_SIGNALS, signal_enabled, signal_code
        from datetime import datetime as _dt

        cr = (getattr(advice, "check_results", None) or {})
        # 信号 → (check_results 键, 触发字段, 中文名)
        signal_map = {
            "stop-hard": ("hard_stop", "triggered", "硬止损"),
            "stop-technical": ("technical_stop", "triggered", "技术止损"),
            "take-left": ("left_side", None, "左侧固定止盈"),
            "take-right": ("right_side", "triggered", "右侧移动止盈"),
            "stop-logic": ("logic_stop", "triggered", "逻辑止损"),
        }
        today = _dt.now().strftime("%Y-%m-%d")
        for signal in ALL_SIGNALS:
            if signal not in signal_map:
                continue
            if not signal_enabled(position.id, signal):
                continue
            key, field, label = signal_map[signal]
            block = cr.get(key)
            if not isinstance(block, dict):
                continue
            if field is not None and not block.get(field):
                continue
            if field is None and not block:
                continue
            finger = f"signal:{position.id}:{signal_code(signal)}:{today}"
            service = NotificationService()
            subject = f"[持仓{label}] {position.stock_code} 触发 {label}建议"
            text = (f"股票: {position.stock_code} {position.stock_name}\n"
                    f"事件: 持仓{label}信号触发\n"
                    f"数据: {cr}")
            event = service.create_event(
                event_type="POSITION_SIGNAL", symbol=position.stock_code,
                subject_type="position_cycle", subject_id=str(position.id),
                priority=1, payload={"subject": subject, "text": text,
                                     "signal": signal, "position_id": position.id},
                data_as_of=today, action=label,
                trigger_fingerprint=finger,
            )
            service.create_rule_delivery(event, template="position_signal")
            logger.info("持仓信号通知已生成: %s %s", position.stock_code, signal)

    # ══════════════════════════════════════════════════
    # 查询 / 总览
    # ══════════════════════════════════════════════════

    def get_summary(self, portfolio_id: int = 1) -> dict:
        """组合总览"""
        positions = self.storage.get_open_positions()
        portfolio = self.storage.get_portfolio(portfolio_id)

        total_market_value = sum(p.market_value for p in positions)
        total_cost = sum(p.total_cost for p in positions)
        total_unrealized = sum(p.unrealized_pnl for p in positions)
        realized = sum(self.storage.realized_pnl(p.id) for p in positions)
        total_invested = sum(p.total_cost for p in positions)

        return {
            "portfolio": portfolio.to_dict(),
            "position_count": len(positions),
            "total_market_value": round(total_market_value, 2),
            "total_cost": round(total_cost, 2),
            "total_unrealized_pnl": round(total_unrealized, 2),
            "total_realized_pnl": round(realized, 2),
            "total_pnl": round(total_unrealized + realized, 2),
            "total_pnl_pct": round(total_unrealized / total_cost * 100, 2) if total_cost > 0 else 0,
        }

    def get_position_detail(self, position_id: int) -> dict:
        """持仓详情 + 交易历史 + 最新建议"""
        p = self.storage.get_position(position_id)
        if p is None:
            raise ValueError(f"持仓不存在: {position_id}")
        txns = self.storage.get_transactions(position_id)
        advice = self.storage.get_latest_advice(position_id)
        advice_history = self.storage.get_advice_history(position_id)
        return {
            "position": p.to_dict(),
            "transactions": [t.to_dict() for t in txns],
            "realized_pnl": self.storage.realized_pnl(position_id),
            "advice": advice.to_dict() if advice else None,
            "advice_history": [a.to_dict() for a in advice_history],
        }

    # ── 自选池 ────────────────────────────────────────

    def add_watchlist(self, stock_code: str, stock_name: str,
                      target_capital: float = 0,
                      asset_type: str = "stock",
                      weak_support: float = 0, strong_support: float = 0,
                      extreme_anchor: float = 0, notes: str = "",
                      added_time: str = "", source: str = "manual") -> WatchlistItem:
        """新增自选/观察（同代码已存在则跳过，去重）。

        added_time 为观察起点，空则默认当天。
        source 为来源: manual 手动(带原因) / holding 持仓 / strategy 策略选入。
        notes 可填加入原因。
        """
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher

        code_norm = StockDataFetcher.normalize_code(stock_code)
        for w in self.storage.get_watchlist():
            if StockDataFetcher.normalize_code(w.stock_code) == code_norm:
                return w  # 已存在，不重复添加
        item = WatchlistItem(
            stock_code=stock_code, stock_name=stock_name,
            asset_type=asset_type, target_capital=target_capital,
            weak_support=weak_support, strong_support=strong_support,
            extreme_anchor=extreme_anchor, notes=notes,
            added_time=added_time or datetime.now().strftime("%Y-%m-%d"),
            source=source,
        )
        return self.storage.add_watchlist(item)

    def update_watchlist_added_time(self, item_id: int, added_time: str):
        """设置观察起点时间（可往前回看；晚于当天视为未生效，由展示层过滤）。"""
        self.storage.update_watchlist_added_time(item_id, added_time)

    def get_watchlist(self) -> list[WatchlistItem]:
        return self.storage.get_watchlist()

    def get_watchlist_codes(self) -> list[str]:
        """自选代码列表（观察池缓存指纹用）。"""
        return [w.stock_code for w in self.storage.get_watchlist()]

    def delete_watchlist(self, item_id: int):
        self.storage.delete_watchlist(item_id)

    # ── 操作记录 / 笔记 ───────────────────────────────

    def update_transaction_note(self, transaction_id: int, reason: str):
        """编辑单笔交易的备注（reason 字段承载笔记）。"""
        txn = self.storage.get_transaction(transaction_id)
        if txn is None:
            raise ValueError(f"交易记录不存在: {transaction_id}")
        self.storage.update_transaction_reason(transaction_id, reason)
        return txn

    def update_position_note(self, position_id: int, notes: str):
        """编辑持仓备注。"""
        p = self.storage.get_position(position_id)
        if p is None:
            raise ValueError(f"持仓不存在: {position_id}")
        p.notes = notes
        self.storage.update_position(p)
        return p

    def update_watchlist_note(self, item_id: int, notes: str):
        """编辑自选备注。"""
        if self.storage.get_watchlist_item(item_id) is None:
            raise ValueError(f"自选不存在: {item_id}")
        self.storage.update_watchlist_notes(item_id, notes)

    def set_watchlist_sim_entry(self, item_id: int, entry_date: str = "",
                                entry_price: float = 0.0):
        """设置自选模拟收益入场点（日期 + 入场价，至少一项）。"""
        item = self.storage.get_watchlist_item(item_id)
        if item is None:
            raise ValueError(f"自选不存在: {item_id}")
        sim_entry = dict(item.sim_entry or {})
        if entry_date:
            sim_entry["date"] = entry_date[:10]
        if entry_price and entry_price > 0:
            sim_entry["price"] = round(float(entry_price), 4)
        elif entry_price is not None and entry_price <= 0:
            sim_entry.pop("price", None)
        self.storage.update_watchlist_sim_entry(item_id, sim_entry)

    def list_transactions(self) -> list[dict]:
        """全量交易流水（含股票名），供操作日志/笔记展示。"""
        rows = []
        for p in self.storage.get_positions():
            for t in self.storage.get_transactions(p.id):
                rows.append({**t.to_dict(),
                             "stock_name": p.stock_name,
                             "stock_code": p.stock_code})
        rows.sort(key=lambda x: (x.get("date") or ""), reverse=True)
        return rows

    def cost_basis(self, position_id: int) -> dict:
        """持仓成本口径：累计净投入 / 当前份额。

        Returns:
            {"net_invested": 累计净投入, "shares": 当前份额,
             "cost_price": 成本价, "buy_total": 累计买入额, "sell_total": 累计卖出额}
        """
        p = self.storage.get_position(position_id)
        if p is None:
            raise ValueError(f"持仓不存在: {position_id}")
        buy_total = 0.0
        sell_total = 0.0
        for t in self.storage.get_transactions(position_id):
            if t.trans_type == TXN_BUY:
                buy_total += t.amount
            elif t.trans_type in (TXN_SELL, TXN_SELL_ALL):
                sell_total += t.amount
        net_invested = buy_total - sell_total
        shares = p.total_shares
        cost_price = net_invested / shares if shares > 0 else 0.0
        return {
            "net_invested": round(net_invested, 2),
            "shares": shares,
            "cost_price": round(cost_price, 4),
            "buy_total": round(buy_total, 2),
            "sell_total": round(sell_total, 2),
        }

    def return_analysis(self) -> list[dict]:
        """构建持仓/自选的收益分析条目列表（含每日明细 DataFrame + 成本基准）。

        持仓：成本 = 累计净投入/份额，起点 = 建仓日，含金额。
        自选：基准 = 观察起点价，起点 = added_time（晚于当天视为未生效），无金额。
        """
        from StockInvestmentTool.portfolio.monitor import PriceMonitor
        from StockInvestmentTool.analysis.returns import compute_returns
        import pandas as pd

        monitor = PriceMonitor()
        today = datetime.now().strftime("%Y-%m-%d")
        entries = []

        # 持仓
        for p in self.storage.get_open_positions():
            try:
                cb = self.cost_basis(p.id)
                kline, _ = monitor.fetch_context_data(p.stock_code)
                start = p.buy_date
                r = compute_returns(kline, start_date=start,
                                    cost_price=cb["cost_price"], shares=cb["shares"])
                last = r.iloc[-1] if r is not None and not r.empty else {}
                has = r is not None and not r.empty
                entries.append({
                    "code": p.stock_code, "name": p.stock_name, "kind": "position",
                    "start_date": start, "cost_price": cb["cost_price"],
                    "shares": cb["shares"], "returns": r,
                    "latest_close": float(last.get("close", 0)) if has else None,
                    "ret_pct": float(last.get("ret_pct", 0)) if has else 0,
                    "ret_amount": float(last.get("ret_amount", 0)) if has else 0,
                })
            except Exception as e:
                logger.warning("收益分析(持仓)失败 %s: %s", p.stock_code, e)

        # 自选/观察（无份额，基准为观察起点价）
        for w in self.storage.get_watchlist():
            try:
                at = w.added_time or today
                if at > today:
                    # 观察起点晚于当天 → 未生效，等开始时间再生成
                    entries.append({
                        "code": w.stock_code, "name": w.stock_name, "kind": "watch",
                        "start_date": at, "cost_price": None, "shares": 0,
                        "returns": pd.DataFrame(), "latest_close": None,
                        "ret_pct": 0, "ret_amount": 0, "pending": True,
                    })
                    continue
                kline, _ = monitor.fetch_context_data(w.stock_code)
                r = compute_returns(kline, start_date=at, cost_price=None, shares=0)
                last = r.iloc[-1] if r is not None and not r.empty else {}
                has = r is not None and not r.empty
                entries.append({
                    "code": w.stock_code, "name": w.stock_name, "kind": "watch",
                    "start_date": at, "cost_price": None, "shares": 0, "returns": r,
                    "latest_close": float(last.get("close", 0)) if has else None,
                    "ret_pct": float(last.get("ret_pct", 0)) if has else 0,
                    "ret_amount": 0,
                })
            except Exception as e:
                logger.warning("收益分析(自选)失败 %s: %s", w.stock_code, e)

        return entries

    # ══════════════════════════════════════════════════
    # Simulation（模拟快照）
    # ══════════════════════════════════════════════════

    def simulate(self, stock_code: str, stock_name: str,
                 scheme_name: str = "default_value",
                 stock_type: str = "B") -> Simulation:
        """对标的跑一次模拟：拉数据算点位，存快照（同股票覆盖写）。"""
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher

        stock_code = StockDataFetcher.normalize_code(stock_code)
        scheme = self.registry.get(scheme_name)
        snapshot = self._build_snapshot(stock_code, scheme, stock_type)
        return self.storage.upsert_simulation(Simulation(
            stock_code=stock_code, stock_name=stock_name,
            scheme_name=scheme_name, stock_type=stock_type,
            snapshot=snapshot,
        ))

    def _build_snapshot(self, code: str, scheme: SchemeConfig,
                        stock_type: str) -> dict:
        """生成模拟点位快照（口径与持仓 advisor 一致：假设按现价建仓）。"""
        from StockInvestmentTool.strategy.market_state import dashboard_market_state

        kline, dividend_anchor = self.monitor.fetch_context_data(code)
        ctx = self.advisor.compute_context(kline, dividend_anchor)
        market_state = dashboard_market_state(kline)
        current = float(ctx.current_price or 0)
        yh = float(ctx.year_high or 0)

        def _r(v, nd=2):
            return round(v, nd) if v else None

        return {
            "current_price": _r(current),
            "market_state": market_state,
            "trend": ctx.trend,
            "weak_support": _r(ctx.weak_support),
            "strong_support": _r(ctx.strong_support),
            "ma20": _r(ctx.ma20),
            "year_high": _r(yh),
            "hard_stop": _r(self._compute_stop_loss(scheme, stock_type, current, current)) if current else None,
            "left_side_zone": [_r(yh * 0.9), _r(yh)] if yh else None,
            "hard_cap": _r(yh * 1.05) if yh else None,
            "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    def get_simulations(self) -> list[Simulation]:
        return self.storage.get_simulations()

    def delete_simulation(self, sim_id: int):
        self.storage.delete_simulation(sim_id)

    def remove_watch_pool_item(self, stock_code: str) -> dict:
        """从观察池移除一只非持仓股票的自选与模拟记录。"""
        from StockInvestmentTool.datasource.fetcher import StockDataFetcher

        normalized = StockDataFetcher.normalize_code(stock_code)
        open_positions = self.storage.get_open_positions()
        if any(StockDataFetcher.normalize_code(p.stock_code) == normalized for p in open_positions):
            raise ValueError("持仓股票不能从观察池移除，请先处理持仓")

        watch_ids = [w.id for w in self.storage.get_watchlist()
                     if StockDataFetcher.normalize_code(w.stock_code) == normalized]
        simulation_ids = [s.id for s in self.storage.get_simulations()
                          if StockDataFetcher.normalize_code(s.stock_code) == normalized]
        for item_id in watch_ids:
            self.storage.delete_watchlist(item_id)
        for sim_id in simulation_ids:
            self.storage.delete_simulation(sim_id)
        return {"stock_code": normalized, "watchlist_removed": len(watch_ids),
                "simulations_removed": len(simulation_ids)}

    def get_simulation_by_code(self, stock_code: str) -> Optional[Simulation]:
        return self.storage.get_simulation_by_code(stock_code)

    # ══════════════════════════════════════════════════
    # 工具
    # ══════════════════════════════════════════════════

    def _load_snapshot(self, p: Position) -> Optional[SchemeConfig]:
        """加载方案快照（失败回退当前方案）"""
        from StockInvestmentTool.portfolio.models import load_snapshot_scheme
        scheme = load_snapshot_scheme(p.scheme_snapshot)
        if scheme is None:
            try:
                scheme = self.registry.get(p.scheme_name)
            except Exception:
                scheme = None
        return scheme

    def _compute_stop_loss(self, scheme: Optional[SchemeConfig],
                           stock_type: str, avg_cost: float,
                           peak_price: Optional[float] = None) -> float:
        """计算当前动态止损价：固定比例止损或已激活的保本止损。"""
        rate = 0.10 if stock_type == "E" else 0.15
        if scheme is not None:
            rule = scheme.rule("sell", "hard_stop")
            if rule is not None:
                params = rule.params or {}
                if str(params.get("mode", "fixed")).lower() == "breakeven":
                    activation = (params.get("breakeven_activation_by_type") or {}).get(stock_type, 0.08)
                    return round(avg_cost / (1 + float(activation)), 2)
                by_type = params.get("stop_loss_by_type")
                if isinstance(by_type, dict) and by_type:
                    rate = float(by_type.get(stock_type, 0.10 if stock_type == "E" else 0.15))
            elif scheme.risk.stop_loss_by_type:
                rate = float(scheme.risk.stop_loss_by_type.get(stock_type, 0.10 if stock_type == "E" else 0.15))
        return round(avg_cost * (1 - rate), 2)
