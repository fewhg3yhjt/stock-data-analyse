# -*- coding: utf-8 -*-
"""条件触发器模型 + 存储（FR-3.1 / FR-3.2 / FR-3.3）

设计：
  - 触发器 = 「条件 × 时间/频率 × 渠道」；
  - 条件类型：操作建议(action)/指标阈值(indicator)/价格涨跌幅(price_change)；
  - 配置持久化：`notifier/notify_rules.yaml`（用户可编辑，管理页可视化保存）；
  - 免重启生效：运行中的调度每次读最新配置（见 web/scheduler 重构 + 管理页 no-restart）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from StockInvestmentTool.portfolio.settings import _atomic_write, _env_file

logger = logging.getLogger(__name__)

RULES_PATH = Path(__file__).resolve().parent / "notify_rules.yaml"


@dataclass
class TriggerCondition:
    """一条触发条件。

    type: action / indicator / price_change
    params（按 type）:
      action: {advice_types: [buy_more, partial_sell, sell_all, adjust_stop]}
      indicator: {name: MA20, operator: cross_above/cross_below/above/below, value: 放值或表达式}
      price_change: {direction: up/down, pct: 3}
    """

    type: str = "action"
    params: dict = field(default_factory=dict)

    def describe(self) -> str:
        if self.type == "action":
            return f"操作建议∈{self.params.get('advice_types', [])}"
        if self.type == "indicator":
            return (f"{self.params.get('name')} {self.params.get('operator')} "
                    f"{self.params.get('value')}")
        if self.type == "price_change":
            return f"单日{self.params.get('direction')}幅≥{self.params.get('pct')}%"
        return "未知条件"


@dataclass
class TriggerRule:
    """一条触发器规则。"""

    id: str = ""
    name: str = ""
    enabled: bool = True
    conditions: list = field(default_factory=list)       # [map]，AND 组合
    logic: str = "AND"                                    # AND / OR
    schedule: dict = field(default_factory=dict)          # {mode, interval_minutes, time, day}
    channel: str = "feishu"
    priority: str = "batch"                                # batch / instant
    use_email_to: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "enabled": self.enabled,
            "conditions": [c if isinstance(c, dict) else c.params for c in self.conditions],
            "logic": self.logic, "schedule": self.schedule,
            "channel": self.channel, "priority": self.priority,
            "use_email_to": self.use_email_to,
        }

    @property
    def is_instant(self) -> bool:
        return self.priority == "instant"


# ── 时间/频率配置 schema（供前端渲染）────────────────────────

SCHEDULE_MODES = [
    {"mode": "intraday", "label": "盘中每 N 分钟", "interval_minutes": 10},
    {"mode": "post_close", "label": "盘后定时 HH:MM", "time": "15:35"},
    {"mode": "daily", "label": "每日一次 HH:MM", "time": "15:35"},
]


# ── 存储 / 读取 ─────────────────────────────────────────────

def default_config() -> dict:
    """默认触发器配置（保护内置默认行为）。"""
    return {
        "rules": [
            {
                "id": "actionable_intraday",
                "name": "盘中操作建议（即时）",
                "enabled": True,
                "conditions": [{"type": "action",
                                "params": {"advice_types": ["partial_sell", "sell_all", "buy_more", "adjust_stop"]}}],
                "logic": "AND",
                "schedule": {"mode": "intraday", "interval_minutes": 10},
                "channel": "feishu",
                "priority": "instant",
            },
            {
                "id": "post_close_summary",
                "name": "盘后持仓汇总（批次）",
                "enabled": True,
                "conditions": [{"type": "action",
                                "params": {"advice_types": ["hold", "partial_sell", "sell_all", "buy_more"]}}],
                "logic": "OR",
                "schedule": {"mode": "post_close", "time": "15:35"},
                "channel": "feishu",
                "priority": "batch",
            },
            {
                "id": "price_threshold",
                "name": "自选价格阈值（批次）",
                "enabled": True,
                "conditions": [{"type": "action", "params": {"advice_types": ["hold"]}}],
                "logic": "AND",
                "schedule": {"mode": "post_close", "time": "15:35"},
                "channel": "feishu",
                "priority": "batch",
            },
        ]
    }


def load_triggers(path: Optional[Path | str] = None) -> list[dict]:
    """读取触发器规则列表（不存在则返回默认）。"""
    path = Path(path) if path else RULES_PATH
    if not path.exists():
        return default_config()["rules"]
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("rules") or []
    except Exception as e:
        logger.warning("通知触发器配置读取失败(%s)，退回默认: %s", path, e)
        return default_config()["rules"]


def save_triggers(rules: list[dict], path: Optional[Path | str] = None) -> Path:
    """原子保存触发器配置。"""
    path = Path(path) if path else RULES_PATH
    content = yaml.safe_dump({"rules": rules}, allow_unicode=True, sort_keys=False)
    _atomic_write(path, content)
    logger.info("通知触发器已保存: %s (%d 条)", path, len(rules))
    return path


def enabled_triggers(path: Optional[Path | str] = None) -> list[dict]:
    """返回启用的触发器。"""
    return [r for r in load_triggers(path) if r.get("enabled", True)]


# ── 邮件收件人配置（FR-3.3 补全 email.to）──────────────────

def email_recipients() -> list[str]:
    """读取邮件收件人列表（.env EMAIL_TO，逗号分隔）。"""
    to = os.getenv("EMAIL_TO", "")
    return [x.strip() for x in to.split(",") if x.strip()]


def set_email_recipients(to_list: list[str]) -> None:
    """写邮件收件人（.env EMAIL_TO）。"""
    from StockInvestmentTool.portfolio.settings import _env_file
    from dotenv import set_key

    env = _env_file()
    env.parent.mkdir(parents=True, exist_ok=True)
    set_key(str(env), "EMAIL_TO", ",".join(to_list))


def mail_config_status() -> dict:
    """只返回是否已配置，不回显明文（沿用安全约定）。"""
    from dotenv import dotenv_values
    env = _env_file()
    values = dotenv_values(env) if env.exists() else {}
    return {
        "email_configured": bool(values.get("EMAIL_USER")),
        "email_to": bool(values.get("EMAIL_TO")),
        "email_to_addresses": [x for x in (values.get("EMAIL_TO") or "").split(",") if x.strip()],
        "env_file": str(env),
    }
