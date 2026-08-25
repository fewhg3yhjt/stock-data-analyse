"""持仓状态机（FR-1.2 去重）

设计意图（HLD §3.2.2 / 状态模式 + 显式事件集 / 转移表统一）：
  - 状态机转移规则**单一定义**，回测与实盘共用；
  - 事件来源不同：回测来自逐 bar 卖出逻辑，实盘来自 advisor 检查链 + 用户手动交易；
    副作用（更新 shares / 记交易）在各侧执行，状态机只负责「状态叫什么、往哪转」。

状态流转（HLD §1.3 术语表）：
    accumulating → holding → left_side → right_side → closed，closed 可 reopen。

事件集：bought / left_tp / breakout / stop / closed / reopen。
"""

from __future__ import annotations

from typing import Optional

# 状态常量（对齐 portfolio/models.py 的 PHASE_*）
STATE_ACCUMULATING = "accumulating"
STATE_HOLDING = "holding"
STATE_LEFT_SIDE = "left_side"
STATE_RIGHT_SIDE = "right_side"
STATE_CLOSED = "closed"

ALL_STATES = (STATE_ACCUMULATING, STATE_HOLDING,
              STATE_LEFT_SIDE, STATE_RIGHT_SIDE, STATE_CLOSED)

# 事件常量
EVENT_BOUGHT = "bought"        # 建仓/加仓至满仓
EVENT_LEFT_TP = "left_tp"      # 触发左侧止盈（部分卖出）
EVENT_BREAKOUT = "breakout"    # 突破前高 → 转右侧
EVENT_STOP = "stop"            # 止损清仓
EVENT_CLOSED = "closed"        # 右侧/任意清仓
EVENT_REOPEN = "reopen"        # 平仓后再建仓

ALL_EVENTS = (EVENT_BOUGHT, EVENT_LEFT_TP, EVENT_BREAKOUT,
              EVENT_STOP, EVENT_CLOSED, EVENT_REOPEN)


class PositionStateMachine:
    """持仓状态机（转移表统一，无内部状态，纯函数式）。

    用法：
        sm = PositionStateMachine()
        next_state = sm.transition("holding", "left_tp")   # → left_side
        possible = sm.next_states("accumulating")          # 可从该状态触发的事件
    """

    TRANSITIONS: dict[tuple[str, str], str] = {
        (STATE_ACCUMULATING, EVENT_BOUGHT): STATE_HOLDING,
        (STATE_ACCUMULATING, EVENT_LEFT_TP): STATE_LEFT_SIDE,
        (STATE_ACCUMULATING, EVENT_STOP): STATE_CLOSED,
        (STATE_HOLDING, EVENT_LEFT_TP): STATE_LEFT_SIDE,
        (STATE_HOLDING, EVENT_BREAKOUT): STATE_RIGHT_SIDE,
        (STATE_HOLDING, EVENT_STOP): STATE_CLOSED,
        (STATE_LEFT_SIDE, EVENT_BREAKOUT): STATE_RIGHT_SIDE,
        (STATE_LEFT_SIDE, EVENT_STOP): STATE_CLOSED,
        (STATE_LEFT_SIDE, EVENT_CLOSED): STATE_CLOSED,
        (STATE_RIGHT_SIDE, EVENT_STOP): STATE_CLOSED,
        (STATE_RIGHT_SIDE, EVENT_CLOSED): STATE_CLOSED,
        (STATE_CLOSED, EVENT_REOPEN): STATE_ACCUMULATING,
    }

    def transition(self, current: str, event: str) -> str:
        """执行一次状态转移，返回新状态。

        Raises:
            ValueError: 非法的 (current, event) 组合。
        """
        if current not in ALL_STATES:
            raise ValueError(f"未知持仓状态: {current!r}")
        key = (current, event)
        if key not in self.TRANSITIONS:
            raise ValueError(f"非法状态转移: {current!r} --{event}--> ?")
        return self.TRANSITIONS[key]

    def can(self, current: str, event: str) -> bool:
        return (current, event) in self.TRANSITIONS

    def next_states(self, current: str) -> list[str]:
        """当前状态可达的事件。"""
        return [e for (s, e) in self.TRANSITIONS if s == current]

    # ── 便捷封装 ──────────────────────────────────────────

    @staticmethod
    def event_for(action: str) -> str:
        """把一次卖出动作映射为状态机事件。"""
        mapping = {
            "clear": EVENT_CLOSED,
            "partial_sell": EVENT_LEFT_TP,
            "transition": EVENT_BREAKOUT,
            "hold": "",
        }
        return mapping.get(action, "")
