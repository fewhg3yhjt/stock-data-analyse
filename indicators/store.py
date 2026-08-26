"""User-managed indicator definitions.

Built-in indicators stay in ``schemes/indicators.yaml`` and are immutable from
the Web UI. User-defined base/composite indicators live in the separate custom
file so editing them cannot overwrite the shipped indicator catalogue.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,47}$")
_CUSTOM_FILE = "indicators.yaml"
_STATE_FILE = ".indicator_state.json"
_VERSIONS_FILE = "indicators.json"
_BUILTIN_KINDS = {"base", "composite", "code", "decision"}


def _root() -> Path:
    return Path(__file__).resolve().parent.parent / "schemes" / "custom"


def custom_path() -> Path:
    d = _root()
    d.mkdir(parents=True, exist_ok=True)
    return d / _CUSTOM_FILE


def state_path() -> Path:
    return _root() / _STATE_FILE


def versions_path() -> Path:
    d = _root() / ".versions"
    d.mkdir(parents=True, exist_ok=True)
    return d / _VERSIONS_FILE


def _read_custom() -> list[dict]:
    path = custom_path()
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return [dict(item) for item in (data.get("indicators") or []) if isinstance(item, dict)]
    except Exception as exc:
        logger.warning("用户指标配置加载失败: %s", exc)
        return []


def _read_state() -> dict:
    path = state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
        # The Web container may run as root; keep runtime indicator config
        # readable for host-side backup and inspection.
        os.chmod(path, 0o644)
    finally:
        tmp.unlink(missing_ok=True)


def _write_custom(items: list[dict]) -> None:
    content = yaml.safe_dump({"indicators": items}, allow_unicode=True, sort_keys=False)
    _write_atomic(custom_path(), content)


def _record_version(name: str, item: dict) -> None:
    history = []
    path = versions_path()
    try:
        if path.exists():
            history = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        history = []
    history.append({"name": name, "ts": time.time(), "item": item})
    _write_atomic(path, json.dumps(history[-50:], ensure_ascii=False, indent=2))


def _builtin_names() -> set[str]:
    from StockInvestmentTool.config import Config

    names = {"MA5", "MA10", "MA20", "MA60", "MA120", "MA240"}
    path = Config.BASE_DIR / "schemes" / "indicators.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for kind in ("bases", "composite", "code"):
            names.update(str(item.get("name")) for item in (data.get(kind) or []) if item.get("name"))
    except Exception as exc:
        logger.warning("内置指标列表读取失败: %s", exc)
    return names


def list_indicators() -> list[dict]:
    """Return custom definitions including disabled entries."""
    state = _read_state()
    result = []
    for item in _read_custom():
        name = str(item.get("name", ""))
        if not name:
            continue
        result.append({
            "name": name,
            "kind": item.get("kind", "composite"),
            "expr": item.get("expr", ""),
            "description": item.get("description", ""),
            "applies_to": item.get("applies_to", ["stock", "etf"]),
            "enabled": bool(state.get(name, {}).get("enabled", item.get("enabled", True))),
            "source": "custom",
            "editable": True,
        })
    return result


def _validate_item(name: str, kind: str, expr: str, description: str) -> dict:
    if not _NAME_RE.fullmatch(name or ""):
        raise ValueError("指标名必须以字母开头，只能包含字母、数字和下划线，长度 2-48")
    if kind not in ("base", "composite"):
        raise ValueError("网页指标只支持 base 或 composite 类型")
    if not expr or len(expr) > 200:
        raise ValueError("指标表达式不能为空且不能超过 200 个字符")
    if "__" in expr:
        raise ValueError("指标表达式含非法字符")
    if len(description or "") > 200:
        raise ValueError("指标说明不能超过 200 个字符")
    builtin = _builtin_names()
    if name in builtin:
        raise ValueError(f"内置指标 {name} 受保护，不可覆盖")

    # Execute against deterministic sample data. This validates syntax,
    # function whitelist and references to existing indicators together.
    from StockInvestmentTool.indicators.engine import IndicatorRegistry
    dates = pd.bdate_range("2025-01-02", periods=300)
    close = pd.Series(range(20, 320), dtype=float)
    sample = pd.DataFrame({
        "date": dates, "open": close, "high": close + 1,
        "low": close - 1, "close": close, "volume": 1000.0,
    })
    IndicatorRegistry().evaluate_expression(sample, expr)
    return {
        "name": name, "kind": kind, "expr": expr,
        "description": description or "", "applies_to": ["stock", "etf"],
    }


def save_indicator(name: str, kind: str, expr: str, description: str = "") -> dict:
    item = _validate_item(name.strip(), kind.strip(), expr.strip(), description.strip())
    items = _read_custom()
    existing = next((x for x in items if x.get("name") == item["name"]), None)
    if existing:
        item["enabled"] = existing.get("enabled", True)
    else:
        item["enabled"] = True
    items = [x for x in items if x.get("name") != item["name"]]
    items.append(item)
    _write_custom(items)
    _record_version(item["name"], item)
    return {**item, "source": "custom", "editable": True, "versioned": True}


def set_enabled(name: str, enabled: bool) -> dict:
    items = _read_custom()
    found = False
    for item in items:
        if item.get("name") == name:
            item["enabled"] = bool(enabled)
            found = True
    if not found:
        raise ValueError(f"用户指标不存在: {name}")
    _write_custom(items)
    state = _read_state()
    state[name] = {"enabled": bool(enabled)}
    _write_atomic(state_path(), json.dumps(state, ensure_ascii=False, indent=2))
    return {"name": name, "enabled": bool(enabled)}


def delete_indicator(name: str) -> bool:
    if name in _builtin_names():
        raise ValueError(f"内置指标 {name} 受保护，不可删除")
    items = _read_custom()
    remaining = [x for x in items if x.get("name") != name]
    if len(remaining) == len(items):
        return False
    _write_custom(remaining)
    state = _read_state()
    state.pop(name, None)
    _write_atomic(state_path(), json.dumps(state, ensure_ascii=False, indent=2))
    return True
