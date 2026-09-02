"""证券交易费用计算。

当前费率：普通股票佣金万 0.85（最低 1 元）、过户费万 0.1、卖出印花税万 5；
境内 ETF 佣金万 0.5（最低 0.5 元），不收股票印花税和过户费。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TradeFees:
    commission: float
    transfer: float
    stamp_tax: float

    @property
    def total(self) -> float:
        return round(self.commission + self.transfer + self.stamp_tax, 2)


STOCK_COMMISSION_RATE = 0.000085
STOCK_MIN_COMMISSION = 1.0
STOCK_TRANSFER_RATE = 0.00001
STOCK_STAMP_RATE = 0.0005
ETF_COMMISSION_RATE = 0.00005
ETF_MIN_COMMISSION = 0.5


def calculate_trade_fees(amount: float, *, stock_type: str = "B",
                         direction: str = "buy") -> TradeFees:
    """计算一笔买卖费用；amount 为成交金额，direction 为 buy/sell。"""
    amount = max(0.0, float(amount or 0))
    if str(stock_type or "").upper() == "E":
        commission = max(amount * ETF_COMMISSION_RATE, ETF_MIN_COMMISSION) if amount else 0.0
        return TradeFees(round(commission, 2), 0.0, 0.0)
    commission = max(amount * STOCK_COMMISSION_RATE, STOCK_MIN_COMMISSION) if amount else 0.0
    transfer = amount * STOCK_TRANSFER_RATE
    stamp_tax = amount * STOCK_STAMP_RATE if str(direction).lower() in {"sell", "sell_all"} else 0.0
    return TradeFees(round(commission, 2), round(transfer, 2), round(stamp_tax, 2))


def fee_breakdown(amount: float, *, stock_type: str = "B", direction: str = "buy") -> dict:
    fees = calculate_trade_fees(amount, stock_type=stock_type, direction=direction)
    return {"commission": fees.commission, "transfer": fees.transfer,
            "stamp_tax": fees.stamp_tax, "total": fees.total}
