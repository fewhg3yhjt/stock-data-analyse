"""风险控制 — 止损 + 回撤止盈"""

from typing import Optional


class RiskController:
    """风控检查器（纯函数，不持状态）"""

    def __init__(
        self,
        stop_loss_rate: float = 0.10,
        drawdown_stop: float = 0.08,
        min_profit_for_dd: float = 0.06,
    ):
        """
        Parameters
        ----------
        stop_loss_rate : float
            硬止损阈值，默认 -10%
        drawdown_stop : float
            回撤止盈阈值，默认 8%
        min_profit_for_dd : float
            回撤止盈生效所需的最小利润阈值，默认 +6%
        """
        self.stop_loss_rate = stop_loss_rate
        self.drawdown_stop = drawdown_stop
        self.min_profit_for_dd = min_profit_for_dd

    def check_stop_loss(self, avg_cost: float, current_price: float) -> bool:
        """硬止损检查：亏损超过阈值即触发"""
        if avg_cost <= 0:
            return False
        return current_price <= avg_cost * (1 - self.stop_loss_rate)

    def check_drawdown(self, peak_price: float, current_price: float,
                       avg_cost: float) -> tuple[bool, str]:
        """回撤止盈检查：从高点回撤超过阈值且已有足够利润时触发

        Returns
        -------
        (triggered: bool, reason: str)
        """
        if avg_cost <= 0 or peak_price <= 0:
            return False, ""
        # 必须有足够的利润才启用回撤止盈
        if peak_price <= avg_cost * (1 + self.min_profit_for_dd):
            return False, ""
        dd = (peak_price - current_price) / peak_price
        if dd >= self.drawdown_stop:
            return True, f"回撤止盈(清仓) 回撤{dd * 100:.1f}%"
        return False, ""

    def position_sizing(self, stage: int, total_cash: float,
                        buy_ratios: list[float]) -> float:
        """单批买入金额计算"""
        if stage < 0 or stage >= len(buy_ratios):
            return 0.0
        return total_cash * buy_ratios[stage]
