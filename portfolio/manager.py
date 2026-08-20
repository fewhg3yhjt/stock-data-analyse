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

logger = logging.getLogger(__name__)


class PortfolioManager:
    """组合管理器"""

    def __init__(self, storage: Optional[PortfolioStorage] = None,
                 registry: Optional[SchemeRegistry] = None):
        self.storage = storage or PortfolioStorage()
        self.registry = registry or SchemeRegistry()
        self.advisor = PostPurchaseAdvisor(self.storage, self.registry)
        self.monitor = PriceMonitor()

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
        from StockInvestmentTool.data.fetcher import StockDataFetcher

        code_norm = StockDataFetcher.normalize_code(stock_code)
        for p in self.storage.get_open_positions():
            if StockDataFetcher.normalize_code(p.stock_code) == code_norm:
                raise ValueError(
                    f"{stock_name} 已有持仓(#{p.id})，加仓请走交易流水"
                )

        scheme = self.registry.get(scheme_name)
        code = stock_code

        # 拉取初始数据计算参考价
        kline, dividend_anchor = self.monitor.fetch_context_data(code)
        last = kline.iloc[-1] if len(kline) else None
        current_price = float(last["close"]) if last is not None else cost

        position = Position(
            portfolio_id=portfolio_id,
            stock_code=code,
            stock_name=stock_name,
            stock_type=stock_type,
            scheme_name=scheme_name,
            scheme_snapshot=snapshot_scheme(scheme),
            total_shares=shares,
            avg_cost=cost,
            total_cost=shares * cost,
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
        position = self.storage.create_position(position)

        # 初始交易记录
        self.storage.add_transaction(Transaction(
            position_id=position.id, trans_type=TXN_BUY,
            date=buy_date, price=cost, shares=shares,
            amount=shares * cost, reason="建仓",
        ))

        # 扣除现金
        self.storage.adjust_cash(-shares * cost, portfolio_id)

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

    def _ensure_watchlist(self, stock_code: str, stock_name: str) -> Optional[WatchlistItem]:
        """确保股票在自选列表（去重）。"""
        from StockInvestmentTool.data.fetcher import StockDataFetcher

        code_norm = StockDataFetcher.normalize_code(stock_code)
        for w in self.storage.get_watchlist():
            if StockDataFetcher.normalize_code(w.stock_code) == code_norm:
                return w
        return self.storage.add_watchlist(
            WatchlistItem(stock_code=stock_code, stock_name=stock_name)
        )

    def sync_holdings_to_watchlist(self) -> int:
        """把所有 open 持仓同步进自选（幂等去重），返回新增数。

        用于回填功能上线前的既有持仓，以及持仓页访问时的兜底。
        """
        from StockInvestmentTool.data.fetcher import StockDataFetcher

        existing = {StockDataFetcher.normalize_code(w.stock_code)
                    for w in self.storage.get_watchlist()}
        added = 0
        for p in self.storage.get_open_positions():
            norm = StockDataFetcher.normalize_code(p.stock_code)
            if norm not in existing:
                self.storage.add_watchlist(
                    WatchlistItem(stock_code=p.stock_code, stock_name=p.stock_name)
                )
                existing.add(norm)
                added += 1
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

        txn_pnl = 0.0
        if trans_type == TXN_BUY:
            position = self._apply_buy(position, price, shares, fee)
        elif trans_type in (TXN_SELL, TXN_SELL_ALL):
            position, txn_pnl = self._apply_sell(position, trans_type, price, shares, fee)
        elif trans_type == TXN_DIVIDEND:
            position = self._apply_dividend(position, price, shares, fee)
        elif trans_type == TXN_CORRECTION:
            position = self._apply_correction(position, price, shares, reason)
        else:
            raise ValueError(f"不支持的交易类型: {trans_type}")

        position.last_operated_date = date
        self.storage.update_position(position)

        txn = Transaction(
            position_id=position.id, trans_type=trans_type, date=date,
            price=price, shares=shares, amount=amount, fee=fee,
            pnl=txn_pnl, reason=reason,
        )
        self.storage.add_transaction(txn)
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
        new_total = p.total_shares + shares
        if new_total <= 0:
            raise ValueError("买入后份额必须为正")
        p.avg_cost = (old_cost + price * shares) / new_total
        p.total_shares = new_total
        p.total_cost += price * shares
        p.buy_stage = min(p.buy_stage + 1, 3)
        if p.buy_stage >= 3:
            p.position_phase = PHASE_HOLDING
        else:
            p.position_phase = PHASE_ACCUMULATING
        # 重算止损线
        scheme = self._load_snapshot(p)
        p.stop_loss_price = self._compute_stop_loss(scheme, p.stock_type, p.avg_cost)
        self.storage.adjust_cash(-(price * shares + fee), p.portfolio_id)
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

        # 现金
        self.storage.adjust_cash(price * shares - fee, p.portfolio_id)

        if p.total_shares <= 0:
            # 全部清仓
            p.status = STATUS_CLOSED
            p.position_phase = PHASE_CLOSED
        else:
            # 部分卖出 → 视作左侧止盈（进入 left_side 阶段）
            if p.position_phase in (PHASE_ACCUMULATING, PHASE_HOLDING):
                p.position_phase = PHASE_LEFT_SIDE
                p.left_tier_sold = min(p.left_tier_sold + 1, 2)
            # 已处于 left_side/right_side 则保持

        return p, pnl

    def _apply_dividend(self, p: Position, price: float, shares: float, fee: float) -> Position:
        """分红: 现金增加, 份额/成本不变"""
        amount = price * shares
        self.storage.adjust_cash(amount - fee, p.portfolio_id)
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
            p.position_phase = PHASE_CLOSED
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

    def delete_position(self, position_id: int):
        self.storage.delete_position(position_id)

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
        return advice

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
                      extreme_anchor: float = 0, notes: str = "") -> WatchlistItem:
        """新增自选（同代码已存在则跳过，去重）。"""
        from StockInvestmentTool.data.fetcher import StockDataFetcher

        code_norm = StockDataFetcher.normalize_code(stock_code)
        for w in self.storage.get_watchlist():
            if StockDataFetcher.normalize_code(w.stock_code) == code_norm:
                return w  # 已存在，不重复添加
        item = WatchlistItem(
            stock_code=stock_code, stock_name=stock_name,
            asset_type=asset_type, target_capital=target_capital,
            weak_support=weak_support, strong_support=strong_support,
            extreme_anchor=extreme_anchor, notes=notes,
        )
        return self.storage.add_watchlist(item)

    def get_watchlist(self) -> list[WatchlistItem]:
        return self.storage.get_watchlist()

    def get_watchlist_codes(self) -> list[str]:
        """自选代码列表（观察池缓存指纹用）。"""
        return [w.stock_code for w in self.storage.get_watchlist()]

    def delete_watchlist(self, item_id: int):
        self.storage.delete_watchlist(item_id)

    # ══════════════════════════════════════════════════
    # Simulation（模拟快照）
    # ══════════════════════════════════════════════════

    def simulate(self, stock_code: str, stock_name: str,
                 scheme_name: str = "default_value",
                 stock_type: str = "B") -> Simulation:
        """对标的跑一次模拟：拉数据算点位，存快照（同股票覆盖写）。"""
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
        rate = PostPurchaseAdvisor._stop_loss_rate(scheme, stock_type)
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
            "ma_20": _r(ctx.ma_20),
            "year_high": _r(yh),
            "hard_stop": _r(current * (1 - rate)) if current else None,
            "left_side_zone": [_r(yh * 0.9), _r(yh)] if yh else None,
            "hard_cap": _r(yh * 1.05) if yh else None,
            "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    def get_simulations(self) -> list[Simulation]:
        return self.storage.get_simulations()

    def delete_simulation(self, sim_id: int):
        self.storage.delete_simulation(sim_id)

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
                           stock_type: str, avg_cost: float) -> float:
        """计算硬止损价 = 均价 × (1 - 扣减率)"""
        rate = 0.10 if stock_type == "E" else 0.15
        if scheme is not None:
            rule = scheme.find_sell_rule("hard_stop")
            if rule is not None:
                by_type = (rule.params or {}).get("stop_loss_by_type")
                if isinstance(by_type, dict) and by_type:
                    rate = float(by_type.get(stock_type, 0.10 if stock_type == "E" else 0.15))
            elif scheme.risk.stop_loss_by_type:
                rate = float(scheme.risk.stop_loss_by_type.get(stock_type, 0.10 if stock_type == "E" else 0.15))
        return round(avg_cost * (1 - rate), 2)
