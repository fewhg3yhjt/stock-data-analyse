# -*- coding: utf-8 -*-
"""持仓策略信号通知配置存储。

每个持仓（按 position_id）可勾选若干买卖信号（如 hard_stop / take_right 等），
满足勾选信号时由持仓评估触发邮件通知。只影响通知，不影响策略判断本身。
默认未配置时视为全部启用。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from StockInvestmentTool.config import Config

logger = logging.getLogger(__name__)

# 全部可通知信号（与前端消息通知页签的 checkbox 对应）
ALL_SIGNALS = [
    "buy-support-weak", "buy-support-strong", "buy-extreme", "buy-trend",
    "stop-hard", "stop-technical", "take-left", "take-right", "stop-logic",
]

_CODES = {
    "buy-support-weak": "buy_support_weak",
    "buy-support-strong": "buy_support_strong",
    "buy-extreme": "buy_extreme",
    "buy-trend": "buy_trend",
    "stop-hard": "stop_hard",
    "stop-technical": "stop_technical",
    "take-left": "take_left",
    "take-right": "take_right",
    "stop-logic": "stop_logic",
}


def _path() -> Path:
    return Config.DATA_DIR / "notify_signal_config.json"


def _load_all() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        logger.warning("信号通知配置读取失败: %s", exc)
        return {}


def _save_all(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def get_signals(position_id: int) -> list[str]:
    """返回该持仓启用的信号；未配置时返回全部默认启用。"""
    return _load_all().get(str(position_id))


def save_signals(position_id: int, signals: list[str]) -> dict:
    """保存该持仓的启用信号集合；返回最终配置。"""
    data = _load_all()
    valid = [s for s in signals if s in ALL_SIGNALS] if signals else []
    if valid:
        data[str(position_id)] = valid
    else:
        data[str(position_id)] = []
    _save_all(data)
    logger.info("持仓 %s 通知信号已保存: %s", position_id, valid)
    return {"position_id": position_id, "signals": valid}


def signal_enabled(position_id: int, signal: str) -> bool:
    """判断某信号是否启用。默认（未配置）视为启用。"""
    signals = get_signals(position_id)
    if signals is None:
        return True
    return signal in signals


def signal_code(signal: str) -> str:
    return _CODES.get(signal, signal)


def to_signal_code(signal: str) -> str:
    """前端 signal 名 → 后端事件类型后缀（兼容）。"""
    return _CODES.get(signal, signal)
