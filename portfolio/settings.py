# -*- coding: utf-8 -*-
"""管理页配置读写 — 策略方案 / 初筛规则 / 通知配置

安全约定:
    - YAML 保存前必须 yaml.safe_load 校验通过，写临时文件再原子替换
    - webhook 密钥只做存在性判断（已配置✓/未配置），不回显明文
    - webhook 保存到 .env（dotenv set_key），不落进前端回显
"""

import logging
import os
from pathlib import Path

import yaml

from StockInvestmentTool.config import Config

logger = logging.getLogger(__name__)

SCHEMES_DIR = Path(__file__).resolve().parent.parent / "schemes"
SCREEN_RULES = Path(__file__).resolve().parent.parent / "screener" / "screen_rules.yaml"
NOTIFY_RULES = Path(__file__).resolve().parent.parent / "notifier" / "rules.yaml"
ENV_KEYS = ("NOTIFY_CHANNEL", "FEISHU_WEBHOOK_URL", "WECOM_WEBHOOK_URL")


# ── 策略方案 ─────────────────────────────────────────────

def list_schemes() -> list[str]:
    if not SCHEMES_DIR.exists():
        return []
    return sorted(p.stem for p in SCHEMES_DIR.glob("*.yaml"))


def read_scheme(name: str) -> str:
    path = SCHEMES_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"方案不存在: {name}")
    return path.read_text(encoding="utf-8")


def save_scheme(name: str, content: str) -> Path:
    """校验并原子替换方案 YAML。"""
    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        raise ValueError("方案 YAML 顶层必须是映射")
    path = SCHEMES_DIR / f"{name}.yaml"
    _atomic_write(path, content)
    return path


# ── 初筛 / 通知规则 ──────────────────────────────────────

def read_rules(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def save_rules(path: Path, content: str) -> Path:
    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        raise ValueError("规则 YAML 顶层必须是映射")
    _atomic_write(path, content)
    return path


def read_screen_rules() -> str:
    return read_rules(SCREEN_RULES)


def save_screen_rules(content: str) -> Path:
    return save_rules(SCREEN_RULES, content)


def read_notify_rules() -> str:
    return read_rules(NOTIFY_RULES)


def save_notify_rules(content: str) -> Path:
    return save_rules(NOTIFY_RULES, content)


# ── 通知策略设置（notify_settings.yaml，管理台可编辑）───────

NOTIFY_SETTINGS = Path(__file__).resolve().parent.parent / "notifier" / "notify_settings.yaml"


def read_notify_settings() -> str:
    return read_rules(NOTIFY_SETTINGS)


def save_notify_settings(content: str) -> Path:
    return save_rules(NOTIFY_SETTINGS, content)


def load_notify_settings() -> dict:
    """解析 notify_settings.yaml 为 dict（scheduler 用）。"""
    try:
        import yaml
        with open(NOTIFY_SETTINGS, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


# ── 通知 webhook（.env）──────────────────────────────────

def _env_file() -> Path:
    """定位 .env（优先包内，其次仓库根）。"""
    for p in (Path(__file__).resolve().parent.parent / ".env",
              Path(__file__).resolve().parent.parent.parent / ".env"):
        if p.exists():
            return p
    return Path(__file__).resolve().parent.parent / ".env"  # 缺省创建包内


def webhook_status() -> dict:
    """只返回是否已配置，不回显明文。"""
    from dotenv import dotenv_values

    env = _env_file()
    values = dotenv_values(env) if env.exists() else {}
    return {
        "channel": values.get("NOTIFY_CHANNEL", "feishu"),
        "feishu_configured": bool(values.get("FEISHU_WEBHOOK_URL")),
        "wecom_configured": bool(values.get("WECOM_WEBHOOK_URL")),
        "env_file": str(env),
    }


def save_webhook(channel: str, feishu_url: str = "", wecom_url: str = "") -> dict:
    """保存渠道与 webhook（空值=不修改；非空=覆盖）。"""
    from dotenv import set_key

    env = _env_file()
    env.parent.mkdir(parents=True, exist_ok=True)
    channel = "feishu" if channel in ("feishu", "lark") else "wecom"
    set_key(str(env), "NOTIFY_CHANNEL", channel)
    if feishu_url:
        set_key(str(env), "FEISHU_WEBHOOK_URL", feishu_url.strip())
    if wecom_url:
        set_key(str(env), "WECOM_WEBHOOK_URL", wecom_url.strip())
    logger.info("webhook 配置已保存: %s", env)
    return webhook_status()


# ── 工具 ─────────────────────────────────────────────────

def _atomic_write(path: Path, content: str):
    """校验通过后写临时文件再原子替换。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
