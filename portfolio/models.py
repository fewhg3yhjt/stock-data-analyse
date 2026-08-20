"""持仓管理数据模型

核心实体:
  - Position: 一只股票的持仓记录（含方案快照、状态机）
  - Transaction: 一笔交易记录（buy/sell/sell_all/dividend/correction）
  - Portfolio: 投资组合（单一默认组合，追踪可用资金）
  - WatchlistItem: 自选池条目
  - ActionAdvice: 操作建议（PostPurchaseAdvisor 输出）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from StockInvestmentTool.core.scheme import SchemeConfig

# 持仓状态机
PHASE_ACCUMULATING = "accumulating"   # 建仓中（还有后续批次未买）
PHASE_HOLDING = "holding"             # 满仓持有
PHASE_LEFT_SIDE = "left_side"         # 左侧止盈进行中
PHASE_RIGHT_SIDE = "right_side"       # 右侧移动止盈
PHASE_CLOSED = "closed"               # 已平仓

# 持仓状态
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"

# 交易类型
TXN_BUY = "buy"
TXN_SELL = "sell"
TXN_SELL_ALL = "sell_all"
TXN_DIVIDEND = "dividend"
TXN_CORRECTION = "correction"

# 建议类型
ADVICE_BUY_MORE = "buy_more"
ADVICE_PARTIAL_SELL = "partial_sell"
ADVICE_SELL_ALL = "sell_all"
ADVICE_HOLD = "hold"
ADVICE_ADJUST_STOP = "adjust_stop"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Position:
    """持仓记录"""

    id: int = 0
    portfolio_id: int = 1
    stock_code: str = ""
    stock_name: str = ""
    stock_type: str = "B"            # A高成长/B价值白马/C强周期/D深度价值
    scheme_name: str = ""
    scheme_snapshot: dict = field(default_factory=dict)

    # 持仓状态
    total_shares: float = 0.0
    avg_cost: float = 0.0
    total_cost: float = 0.0          # 当前持仓的累计成本（买入总金额 - 已卖成本）
    current_price: float = 0.0
    peak_price: float = 0.0
    position_phase: str = PHASE_ACCUMULATING
    buy_stage: int = 0               # 已完成买入批次 (0-3)
    left_tier_sold: int = 0          # 左侧已卖档位 (0-2)
    stop_loss_price: float = 0.0

    buy_date: str = ""
    last_operated_date: str = ""
    status: str = STATUS_OPEN
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    # ── 派生属性 ─────────────────────────────────────

    @property
    def market_value(self) -> float:
        """当前市值"""
        return round(self.total_shares * self.current_price, 2)

    @property
    def unrealized_pnl(self) -> float:
        """浮动盈亏"""
        return round((self.current_price - self.avg_cost) * self.total_shares, 2)

    @property
    def unrealized_pnl_pct(self) -> float:
        """浮动盈亏率"""
        if self.avg_cost <= 0:
            return 0.0
        return round((self.current_price / self.avg_cost - 1) * 100, 2)

    @property
    def is_open(self) -> bool:
        return self.status == STATUS_OPEN and self.total_shares > 0

    def to_dict(self) -> dict:
        """转 JSON 安全 dict（供 Web 展示）"""
        return {
            "id": self.id,
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "stock_type": self.stock_type,
            "scheme_name": self.scheme_name,
            "total_shares": self.total_shares,
            "avg_cost": self.avg_cost,
            "total_cost": self.total_cost,
            "current_price": self.current_price,
            "peak_price": self.peak_price,
            "position_phase": self.position_phase,
            "buy_stage": self.buy_stage,
            "left_tier_sold": self.left_tier_sold,
            "stop_loss_price": self.stop_loss_price,
            "buy_date": self.buy_date,
            "last_operated_date": self.last_operated_date,
            "status": self.status,
            "notes": self.notes,
            # 派生
            "market_value": self.market_value,
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
            "is_open": self.is_open,
        }


@dataclass
class Transaction:
    """交易记录"""

    id: int = 0
    position_id: int = 0
    trans_type: str = ""             # buy/sell/sell_all/dividend/correction
    date: str = ""
    price: float = 0.0
    shares: float = 0.0
    amount: float = 0.0
    fee: float = 0.0
    pnl: float = 0.0                 # 仅 sell 时有效
    reason: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "position_id": self.position_id,
            "trans_type": self.trans_type,
            "date": self.date,
            "price": self.price,
            "shares": self.shares,
            "amount": self.amount,
            "fee": self.fee,
            "pnl": self.pnl,
            "reason": self.reason,
            "created_at": self.created_at,
        }


@dataclass
class Portfolio:
    """投资组合"""

    id: int = 1
    name: str = "默认组合"
    cash_available: float = 0.0
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "cash_available": self.cash_available,
            "created_at": self.created_at,
        }


@dataclass
class WatchlistItem:
    """自选池条目"""

    id: int = 0
    stock_code: str = ""
    stock_name: str = ""
    asset_type: str = "stock"        # stock / etf
    target_capital: float = 0.0      # 计划总仓位（元）
    weak_support: float = 0.0        # 手动预设弱支撑（可选）
    strong_support: float = 0.0      # 手动预设强支撑（可选）
    extreme_anchor: float = 0.0      # 手动预设极端低估锚（可选）
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "asset_type": self.asset_type,
            "target_capital": self.target_capital,
            "weak_support": self.weak_support,
            "strong_support": self.strong_support,
            "extreme_anchor": self.extreme_anchor,
            "notes": self.notes,
        }


@dataclass
class Simulation:
    """模拟快照 — 对标的用分析逻辑跑出的止盈止损点位（自选与持仓之间的中间态）

    snapshot: JSON 存点位（current_price/market_state/hard_stop/left_side_zone/
              right_side/补仓支撑/year_high 等），由 manager 生成。
    """

    id: int = 0
    stock_code: str = ""
    stock_name: str = ""
    scheme_name: str = "default_value"
    stock_type: str = "B"
    snapshot: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            **self.snapshot,
            "id": self.id,
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "scheme_name": self.scheme_name,
            "stock_type": self.stock_type,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class ActionAdvice:
    """操作建议（PostPurchaseAdvisor 输出）"""

    id: int = 0
    position_id: int = 0
    stock_code: str = ""
    stock_name: str = ""
    advice_type: str = ADVICE_HOLD    # buy_more/partial_sell/sell_all/hold/adjust_stop
    urgency: str = "normal"           # normal/attention/urgent
    reason: str = ""
    suggested_price: float = 0.0
    suggested_shares: float = 0.0
    suggested_amount: float = 0.0
    check_results: dict = field(default_factory=dict)  # 各规则检查明细
    created_at: str = ""

    @property
    def is_actionable(self) -> bool:
        """是否有实际操作（非 hold）"""
        return self.advice_type in (ADVICE_BUY_MORE, ADVICE_PARTIAL_SELL, ADVICE_SELL_ALL, ADVICE_ADJUST_STOP)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "position_id": self.position_id,
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "advice_type": self.advice_type,
            "urgency": self.urgency,
            "reason": self.reason,
            "suggested_price": self.suggested_price,
            "suggested_shares": self.suggested_shares,
            "suggested_amount": self.suggested_amount,
            "check_results": self.check_results,
            "created_at": self.created_at,
            "is_actionable": self.is_actionable,
        }


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def snapshot_scheme(scheme: SchemeConfig) -> dict:
    """将方案配置转为可 JSON 序列化的快照"""
    return scheme.to_dict()


def load_snapshot_scheme(snapshot: dict) -> Optional[SchemeConfig]:
    """从快照 dict 恢复 SchemeConfig（供 Advisor 使用）

    Returns:
        SchemeConfig 或 None（快照无效时）
    """
    if not snapshot or not isinstance(snapshot, dict):
        return None
    try:
        from StockInvestmentTool.core.scheme import load_scheme_from_dict
        return load_scheme_from_dict(snapshot)
    except Exception:
        return None


def json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def json_loads(s: str) -> dict:
    if not s:
        return {}
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return {}
