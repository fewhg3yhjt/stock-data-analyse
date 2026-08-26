"""V6.0 单股回测引擎 — 逐 K 线把派生值喂给纯函数判定器

对齐《四维一体实战投资体系 V6.0》与实验方案 Phase 3（单股对比）：
  买入: judge_market_state(六态) → judge_buy_tree(5前置 + 分支 + 批次结构)
  卖出: judge_sell_tree_v6(逻辑 > 价格贝塔 > 时间 > 三层止盈) + 止盈硬上限
  仓位: judge_buy_tree 返回 position_pct → 目标仓位 × 初始资金

本引擎是「纯机械状态机」：所有决策逻辑都在 strategy/ 下的纯函数里，
引擎只负责逐 bar 计算派生值并喂入，因此与 v4.5 对比时数据口径完全一致
（同一 K 线、同一指标列、同一支撑位算法、同一股息率锚）。

输出结构与 TakeProfitOptimizer.run_detailed 一致
（initial_cash/final_asset/total_return/trades/equity_curve…），
故 PerformanceMetrics.summary / build_markdown / build_charts 可直接复用。

数据边界（实验方案 §8#5 + 数据阻塞）：
  - screener_pass / valuation_ok / thesis_ok / position_below_macro_cap
    为基本面/组合层信号，K 线回测不可得 → 默认 True（报告中标注）。
  - 沪深300年线熔断(circuit_breaker) 与 贝塔保护 用缓存 HS300 实算；
    HS300 缺失 → 熔断放行(不误杀)、贝塔按"无宏观对冲信号"处理（价格止损照常）。
  - 股债收益差 ERP 依赖宏观（当前拉取挂起）→ None → 硬止损豁免(>5.5%)不触发。
  - 成长(A) 营收加速不可得 → None → 时间止损按"未出现"从严（方案口径）。
    强周期(C) 右侧信号 = 持仓期内曾突破前高（close ≥ 前高）。
  - 最小仓位 250 行预热：ma_250 从第 250 根起有效；此前市场状态按
    market_state_v6 的 data_insufficient 分支降级（不误判强/弱多头）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from StockInvestmentTool.analysis.market_state_v6 import judge_market_state
from StockInvestmentTool.core.scheme import SchemeConfig
from StockInvestmentTool.strategy.buy_tree import judge_buy_tree, BRANCH_FORBID
from StockInvestmentTool.strategy.sell_tree_v6 import (
    judge_sell_tree_v6, take_profit_hard_cap,
    LEFT_SIDE_RATIO_BY_TYPE, RIGHT_DD_BY_TYPE,
)
from StockInvestmentTool.strategy.support import (
    MaSource,
    RollingLowSource,
    RowContext,
    get_support_levels,
)
from StockInvestmentTool.strategy.position_state import (
    PositionStateMachine, STATE_ACCUMULATING, STATE_HOLDING,
    STATE_LEFT_SIDE, STATE_RIGHT_SIDE, STATE_CLOSED,
    EVENT_BOUGHT, EVENT_LEFT_TP, EVENT_BREAKOUT,
)

logger = logging.getLogger(__name__)

# 与 v4.5 的 TakeProfitOptimizer 相同的 year_high 滚动窗口径
_YEAR_HIGH_WINDOW = 252
_YEAR_HIGH_MIN_PERIODS = 60


@dataclass
class _Position:
    """单次持仓状态（再入场时整体重置）"""
    shares: float = 0.0
    total_cost: float = 0.0
    target_capital: float = 0.0
    pending: list = field(default_factory=list)   # 剩余未成交批次 {label,threshold,ratio}
    start_i: int = 0
    branch: str = ""
    left_tiers_sold: int = 0
    right_mode: bool = False
    right_peak: float = 0.0
    right_signal_occurred: bool = False
    csi_entry: Optional[float] = None             # 入场日沪深300收盘
    avg_cost: float = 0.0
    year_high_at_entry: Optional[float] = None    # 入场日的前高（右侧突破判定锚）
    state: str = STATE_CLOSED


class BacktestEngineV6:
    """V6.0 单股回测引擎

    Args:
        df: K 线（已 compute_all，含 ma_5/20/60/250、bias_ratio、year_low、low_3m）
        initial_cash: 初始资金
        stock_type: A/B/C/D/E
        dividend_anchor: 股息率极端低估锚（与 v4.5 相同来源）
        scheme: 可选，读取 left_side_fixed.ratio_by_type /
                right_side_trailing.drawdown_by_type（配置驱动）
        csi300: 沪深300 K 线（可选，贝塔保护/年线熔断；缺省降级）
        warmup: 起始 bar 索引（year_high_rolling 预热，默认 60）
    """

    def __init__(
        self,
        df: pd.DataFrame,
        initial_cash: float = 100_000,
        stock_type: str = "B",
        dividend_anchor: Optional[float] = None,
        scheme: Optional[SchemeConfig] = None,
        csi300: Optional[pd.DataFrame] = None,
        warmup: int = _YEAR_HIGH_MIN_PERIODS,
    ):
        self.df = df.copy()
        self.initial_cash = float(initial_cash)
        self.stock_type = (stock_type or "B").upper()
        self.dividend_anchor = dividend_anchor
        self.scheme = scheme
        self.warmup = int(warmup)
        self.state_machine = PositionStateMachine()

        # 滚动年度最高价（与 v4.5 同一口径，供左侧/右侧止盈与前高突破）
        if "year_high_rolling" not in self.df.columns:
            self.df["year_high_rolling"] = (
                self.df["high"].rolling(_YEAR_HIGH_WINDOW, min_periods=_YEAR_HIGH_MIN_PERIODS)
                .max().bfill()
            )

        # 配置驱动：三层止盈比例 / 右侧回撤阈值（缺省用 sell_tree_v6 模块常量）
        self.left_ratio_by_type = dict(LEFT_SIDE_RATIO_BY_TYPE)
        self.right_dd_by_type = dict(RIGHT_DD_BY_TYPE)
        if scheme is not None:
            lr = scheme.find_sell_rule("left_side_fixed")
            if lr is not None and isinstance(lr.params.get("ratio_by_type"), dict):
                self.left_ratio_by_type = {k: float(v) for k, v in lr.params["ratio_by_type"].items()}
            rr = scheme.find_sell_rule("right_side_trailing")
            if rr is not None and isinstance(rr.params.get("drawdown_by_type"), dict):
                self.right_dd_by_type = {k: float(v) for k, v in rr.params["drawdown_by_type"].items()}

        # 沪深300（贝塔保护 / 年线熔断）：按日期对齐，取 ≤ 当前 bar 的最后一行
        self._csi_index = None
        self._csi_close = None
        self._csi_ma250 = None
        if csi300 is not None and not csi300.empty:
            csi = csi300.copy()
            if "ma_250" not in csi.columns:
                from StockInvestmentTool.datasource.indicators import TechnicalIndicators
                csi = TechnicalIndicators.compute_all(csi)
            csi["date"] = pd.to_datetime(csi["date"])
            csi = csi.drop_duplicates("date").sort_values("date").set_index("date")
            self._csi_index = csi.index
            self._csi_close = csi["close"].to_numpy(dtype=float)
            self._csi_ma250 = csi.get("ma_250", pd.Series(index=csi.index, dtype=float)).to_numpy(dtype=float)

    # ── 支撑位（与 v4.5 _get_support_levels 完全同口径）────────────

    def _get_support_levels(self, row: pd.Series) -> tuple[float, float, float]:
        """(weak_support, strong_support, extreme_anchor) —— FR-1.2 统一骨架。

        强/弱支撑只用价格类（MA60/近3月低点/年内低点），股息锚仅作为极端低估锚。
        """
        sources = [MaSource(60), RollingLowSource(63), RollingLowSource(None)]
        return get_support_levels(
            sources, RowContext(row), row, dividend_anchor=self.dividend_anchor,
        )

    @staticmethod
    def _num(row: pd.Series, key: str) -> Optional[float]:
        v = row.get(key)
        if v is None or pd.isna(v):
            return None
        return float(v)

    # ── 沪深300 对齐 ──────────────────────────────────────

    def _csi_at(self, date) -> Optional[dict]:
        if self._csi_index is None or len(self._csi_index) == 0:
            return None
        ts = pd.Timestamp(date)
        idx = int(self._csi_index.searchsorted(ts, side="right")) - 1
        if idx < 0:
            return None
        return {
            "close": self._csi_close[idx],
            "ma250": self._csi_ma250[idx],
            "date": self._csi_index[idx],
        }

    # ── 主入口 ────────────────────────────────────────────

    def run(self) -> dict:
        """完整回测，返回与 run_detailed 同构的 dict。"""
        cash = self.initial_cash
        pos = _Position()
        trades: list[dict] = []
        equity_curve: list[dict] = []

        df = self.df
        for i in range(self.warmup, len(df)):
            row = df.iloc[i]
            date = row["date"]
            close = float(row["close"])
            low = float(row["low"])
            high = float(row["high"])
            csi = self._csi_at(date)

            bought_this_bar = False

            # ════ 买入：仅空仓时开新仓 ════
            if pos.shares <= 1e-6:
                weak, strong, extreme = self._get_support_levels(row)
                state_res = judge_market_state(df.iloc[: i + 1])
                circuit_ok = True
                if csi is not None and not pd.isna(csi["ma250"]):
                    circuit_ok = csi["close"] >= csi["ma250"]   # 未跌破年线（"跌破"=严格小于）
                res = judge_buy_tree(
                    market_state=state_res.state,
                    stock_type=self.stock_type,
                    price=close,
                    ma20=self._num(row, "ma_20"),
                    weak_support=weak,
                    strong_support=strong,
                    extreme_anchor=extreme,
                    bias_ratio=self._num(row, "bias_ratio"),
                    # 基本面/组合层前置（K线回测不可得 → 放行，见模块 docstring）
                    screener_pass=True,
                    position_below_macro_cap=True,
                    circuit_breaker_ok=circuit_ok,
                    valuation_ok=True,
                    thesis_ok=True,
                )
                if not res.blocked and res.branch != BRANCH_FORBID and res.batches:
                    pos.target_capital = self.initial_cash * res.position_pct / 100.0
                    pos.pending = [
                        {"label": b["label"], "threshold": b["threshold"], "ratio": b["ratio"]}
                        for b in res.batches if b["ratio"] > 0
                    ]
                    pos.start_i = i
                    pos.branch = res.branch
                    pos.csi_entry = csi["close"] if csi is not None else None
                    cash, pos, trades, bought_this_bar = self._execute_pending(
                        row, cash, pos, trades,
                    )
                    if pos.shares > 1e-6 and pos.year_high_at_entry is None:
                        pos.year_high_at_entry = self._num(row, "year_high_rolling") or close

            # ════ 卖出：持仓中 + 非买入当日 ════
            elif not bought_this_bar:
                sold = self._execute_sell(row, i, date, cash, pos, trades, csi)
                if sold is not None:
                    cash, pos = sold
                # 卖出后若清仓，pos.shares≈0，下一 bar 自动回到买入分支

            # ════ 权益曲线 ════
            total_asset = cash + pos.shares * close
            equity_curve.append({
                "date": date, "total_asset": round(total_asset, 2), "price": round(close, 2),
            })

        # 期末平仓
        if pos.shares > 1e-6:
            final_price = float(df["close"].iloc[-1])
            cash += pos.shares * final_price
            trades.append({
                "date": df["date"].iloc[-1],
                "type": "期末平仓",
                "price": round(final_price, 2),
                "shares": round(pos.shares, 2),
                "amount": round(pos.shares * final_price, 2),
                "pnl": round(pos.shares * (final_price - pos.avg_cost), 2),
                "reason": "回测期结束，强制平仓",
            })
            pos.shares = 0.0

        final_asset = cash
        total_return = (final_asset - self.initial_cash) / self.initial_cash * 100
        buy_hold_return = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100

        return {
            "backtest": {
                "initial_cash": self.initial_cash,
                "final_asset": round(final_asset, 2),
                "total_return": round(total_return, 2),
                "buy_hold_return": round(buy_hold_return, 2),
                "excess_return": round(total_return - buy_hold_return, 2),
                "trades": trades,
                "trade_count": len(trades),
                "equity_curve": equity_curve,
                "params": {"engine": "v6_si_wei"},
            },
            "custom_params": {"engine": "v6_si_wei", "market_state_arbiter": True},
        }

    # ── 批次成交记账 ──────────────────────────────────────

    def _execute_pending(self, row, cash, pos: _Position, trades) -> tuple:
        """执行当前 bar 可成交的批次（返回 cash, pos, trades, bought_this_bar）。"""
        low = float(row["low"])
        close = float(row["close"])
        date = row["date"]
        bought = False

        idx = 0
        while idx < len(pos.pending) and cash > 1:
            b = pos.pending[idx]
            if b["label"].startswith("当前价"):
                fill = close
            elif b["threshold"] and b["threshold"] > 0 and low <= b["threshold"]:
                fill = min(b["threshold"], close)
            else:
                break   # 未达该批阈值 → 后续更低批也不触发，保留待后续 bar

            amt = b["ratio"] * pos.target_capital
            if amt > cash:
                amt = cash
            if amt <= 1:
                break

            buy_shares = amt / fill
            pos.shares += buy_shares
            pos.total_cost += buy_shares * fill
            pos.avg_cost = pos.total_cost / pos.shares
            cash -= amt
            bought = True

            trades.append({
                "date": date,
                "type": f"买入({pos.branch}·{b['label']})",
                "price": round(fill, 2),
                "shares": round(buy_shares, 2),
                "amount": round(amt, 2),
                "reason": f"最低价{low:.2f}≤阈值{b['threshold'] or '当前价'}，"
                          f"成交{b['label']}（{b['ratio']:.0%}目标仓位）",
            })
            pos.pending.pop(idx)
            if pos.state == STATE_CLOSED:
                pos.state = STATE_ACCUMULATING
            elif pos.state == STATE_ACCUMULATING and not pos.pending:
                pos.state = self.state_machine.transition(pos.state, EVENT_BOUGHT)

        return cash, pos, trades, bought

    # ── 卖出执行 ──────────────────────────────────────────

    def _execute_sell(self, row, i, date, cash, pos: _Position, trades, csi) -> Optional[tuple]:
        """卖出决策树执行。返回 (cash, pos) 或 None（未卖出时已就地更新峰值）。"""
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        avg_cost = pos.avg_cost
        year_high = self._num(row, "year_high_rolling") or close
        ma250 = self._num(row, "ma_250")
        # 右侧突破锚 = 入场日的前高（避免旧前高滑出 252 窗口后"自然突破"的退化）
        breakout_anchor = pos.year_high_at_entry or year_high

        # 前高突破 → 右侧移动止盈启动（right_peak 从突破日起跟踪）
        if high >= breakout_anchor:
            pos.right_mode = True
            pos.right_signal_occurred = True
        if pos.right_mode:
            pos.right_peak = max(pos.right_peak, close)

        # 沪深300同期跌幅（负=跌）：入场至今
        csi_drop = None
        if csi is not None and pos.csi_entry:
            csi_drop = (csi["close"] - pos.csi_entry) / pos.csi_entry

        decision = judge_sell_tree_v6(
            stock_type=self.stock_type,
            thesis_ok=True,                      # K线回测无法证伪逻辑 → 不触发逻辑止损
            low=low,
            avg_cost=avg_cost,
            csi300_drop_pct=csi_drop,
            erp=None,                            # 宏观阻塞：不触发硬止损豁免
            left_tp_triggered=(pos.left_tiers_sold > 0),
            holding_days=i - pos.start_i,        # 交易日口径（与常量180一致）
            right_signal_occurred=pos.right_signal_occurred,
            revenue_accel=None,                  # 成长信号不可得 → 从严按未出现
            high=high,
            year_high=year_high,
            ma250=ma250,
            right_peak=pos.right_peak,
            close=close,
            left_tiers_sold=pos.left_tiers_sold,
            left_ratio_by_type=self.left_ratio_by_type,
            right_dd_by_type=self.right_dd_by_type,
        )

        # ── 清仓 ──
        if decision.action == "clear":
            cash, pos = self._close_position(close, date, cash, pos, trades, decision)
            return cash, pos

        # ── 部分卖出（时间止损/左侧止盈）──
        if decision.action == "partial_sell":
            ratio = decision.ratio
            fill = high if "止盈" in decision.rule else close   # 左侧限价 / 时间止损收盘
            sell_shares = pos.shares * ratio
            sell_shares = min(sell_shares, pos.shares)
            if sell_shares > 1e-6:
                cash += sell_shares * fill
                pos.total_cost *= max(0.0, 1 - sell_shares / pos.shares)
                pos.shares -= sell_shares
                pos.avg_cost = (pos.total_cost / pos.shares) if pos.shares > 1e-6 else 0.0
                if "第一档" in decision.rule:
                    pos.left_tiers_sold = 2
                elif "预警档" in decision.rule:
                    pos.left_tiers_sold = max(pos.left_tiers_sold, 1)
                trades.append({
                    "date": date,
                    "type": decision.rule,
                    "price": round(fill, 2),
                    "shares": round(sell_shares, 2),
                    "amount": round(sell_shares * fill, 2),
                    "pnl": round(sell_shares * (fill - avg_cost), 2),
                    "reason": decision.reason,
                })
                if pos.shares <= 1e-6:
                    self._reset_position_state(pos)
                    return cash, pos
            return cash, pos

        # ── 持有：止盈硬上限检查（V6 特有）──
        hard = take_profit_hard_cap(year_high, ma250)
        if hard and close >= hard:
            decision = SellDecisionStub("止盈硬上限", f"收盘{close:.2f}≥硬上限{hard:.2f}")
            cash, pos = self._close_position(close, date, cash, pos, trades, decision)
            return cash, pos

        return None

    @staticmethod
    def _reset_position_state(pos: _Position) -> None:
        """整仓重置（再入场起点）。

        再入场必须从零开始：left_tiers_sold / right_mode / right_peak 等若不重置，
        残留的「左侧止盈已触发」会把新一仓的止损线直接上移到成本价（保本线），
        导致每次买入次日就被误止损（"两轮买入"状态机永不收敛）。
        """
        pos.shares = 0.0
        pos.total_cost = 0.0
        pos.avg_cost = 0.0
        pos.target_capital = 0.0
        pos.pending = []
        pos.start_i = 0
        pos.branch = ""
        pos.left_tiers_sold = 0
        pos.right_mode = False
        pos.right_peak = 0.0
        pos.right_signal_occurred = False
        pos.csi_entry = None
        pos.year_high_at_entry = None

    def _close_position(self, fill, date, cash, pos: _Position, trades, decision) -> tuple:
        """清仓：按指定价格卖出全部持仓，并整体重置本轮持仓状态。"""
        shares = pos.shares
        pnl = shares * (fill - pos.avg_cost)
        cash += shares * fill
        trades.append({
            "date": date,
            "type": decision.rule,
            "price": round(fill, 2),
            "shares": round(shares, 2),
            "amount": round(shares * fill, 2),
            "pnl": round(pnl, 2),
            "reason": decision.reason,
        })
        self._reset_position_state(pos)
        return cash, pos


class SellDecisionStub:
    """硬上限触发用的轻量决策（不进入 sell_tree_v6 主决策）。"""
    def __init__(self, rule: str, reason: str):
        self.action = "clear"
        self.rule = rule
        self.reason = reason
