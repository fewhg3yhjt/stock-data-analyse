# -*- coding: utf-8 -*-
"""方案存储管理 — 保存/版本/启停（FR-2.4 / FR-2.5）

设计要点：
  - 用户方案落 `schemes/custom/`（与内置方案分开），由 SchemeRegistry 扫描加载；
  - 原子写入：校验通过才落盘（写临时文件再 os.replace）；
  - 版本历史：每个方案保存历史版本快照（`schemes/custom/.versions/<name>.yaml`），
    可回滚；
  - 启停状态：存在 `schemes/custom/.state.json`（{name: {enabled, default}}），
    SchemeRegistry 加载时据此过滤停用方案与确定默认方案；
  - 内置方案（schemes/ 根目录非 custom 的 yaml）只读，不做启停/版本，保护内置。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import hashlib
from pathlib import Path
from typing import Optional

import yaml

from StockInvestmentTool.config import Config

logger = logging.getLogger(__name__)

# 内置方案判定的来源目录集合（非这些目录的为内置）
_CUSTOM_DIR_NAME = "custom"
_STATE_FILE = ".state.json"
_VERSIONS_DIR = ".versions"

# 内置方案名单（保护这些不可删除/启停）
BUILTIN_SCHEMES = ("default_value", "aggressive_growth", "v6_si_wei")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,63}$")


def _safe_name(name: str) -> str:
    name = str(name or "").strip()
    if not _SAFE_NAME_RE.fullmatch(name):
        raise ValueError("方案名必须以字母开头，只能包含字母、数字、下划线和连字符")
    return name


def _schemes_root() -> Path:
    return Path(__file__).resolve().parent.parent / "schemes"


def custom_dir() -> Path:
    d = _schemes_root() / _CUSTOM_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def versions_dir() -> Path:
    d = custom_dir() / _VERSIONS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── 状态文件（enabled / default）────────────────────────────

def _state_path() -> Path:
    return custom_dir() / _STATE_FILE


def _read_state() -> dict:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("方案状态文件损坏，重置: %s", e)
        return {}


def _write_state(state: dict):
    try:
        path = _state_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        logger.error("方案状态写入失败: %s", e)


# ── 版本历史 ────────────────────────────────────────────────

def _version_file(name: str) -> Path:
    return versions_dir() / f"{_safe_name(name)}.json"


def record_version(name: str, content: str):
    """记录一次版本快照（按时间戳，保留最近 N 份）。"""
    import time
    try:
        vdir = versions_dir()
        entry = {
            "name": name,
            "ts": time.time(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "content": content,
        }
        # 历史文件按方案聚合
        hist = read_versions(name)
        hist.append(entry)
        # 每方案保留最近 20 份
        hist = hist[-20:]
        _version_file(name).write_text(json.dumps(hist, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    except Exception as e:
        logger.error("记录方案版本失败: %s", e)


def read_versions(name: str) -> list[dict]:
    p = _version_file(name)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def rollback_version(name: str, ts: float) -> Optional[str]:
    """回滚到某个历史版本，返回被恢复的 YAML 内容。"""
    for v in read_versions(name):
        if abs(float(v["ts"]) - float(ts)) < 1e-3:
            return v["content"]
    return None


# ── 保存 / 删除 / 启停 ──────────────────────────────────────

def save_scheme(name: str, content: str, *, validate: bool = True) -> dict:
    """校验并原子保存用户方案到 custom/。返回 {name, version, path}。

    校验通过才落盘；同时记录版本快照。
    """
    from StockInvestmentTool.core.composer import validate_yaml

    # 校验（若调用方已单查过可传 validate=False）
    if validate:
        res = validate_yaml(content)
        if not res["ok"]:
            raise ValueError(res["error"])

    data = yaml.safe_load(content)
    name = _safe_name(data.get("name") or name)
    state = _read_state()
    previous = state.get(name, {})
    path = custom_dir() / f"{name}.yaml"

    # 原子写入
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)

    # 记录版本
    record_version(name, content)
    # Editing a published scheme requires a fresh validation of the new content.
    state[name] = {**previous, "state": "draft" if previous.get("state") == "published" else previous.get("state", "draft")}
    _write_state(state)
    logger.info("方案已保存: %s (%s)", name, path)
    return {"name": name, "version": str(data.get("version", "1.0")), "path": str(path),
            "state": state[name].get("state", "draft")}


def metadata(name: str) -> dict:
    name = _safe_name(name)
    path = custom_dir() / f"{name}.yaml"
    if not path.exists():
        raise ValueError(f"用户方案不存在: {name}")
    state = _read_state().get(name, {})
    # Legacy user schemes were already executable; preserve that behavior.
    return {"name": name, "state": state.get("state", "published"),
            "enabled": bool(state.get("enabled", True)),
            "default": bool(state.get("default", False)),
            "validated_at": state.get("validated_at"),
            "validated_by": state.get("validated_by"),
            "validation_sample_code": state.get("validation_sample_code"),
            "validation_result": state.get("validation_result"),
            "published_at": state.get("published_at")}


def set_validation(name: str, *, sample_code: str, result: dict, validated_by: str = "admin") -> dict:
    name = _safe_name(name)
    state = _read_state()
    entry = state.setdefault(name, {})
    entry.update({"state": "validated", "validated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                  "validated_by": validated_by, "validation_sample_code": sample_code,
                  "validation_result": {"hash": content_hash(name), "result": result}})
    _write_state(state)
    return metadata(name)


def content_hash(name: str) -> str:
    name = _safe_name(name)
    return hashlib.sha256((custom_dir() / f"{name}.yaml").read_bytes()).hexdigest()


def publish(name: str) -> dict:
    item = metadata(name)
    if item["state"] != "validated":
        raise ValueError("方案必须先通过样本验证才能发布")
    validation = item.get("validation_result") or {}
    if validation.get("hash") != content_hash(name):
        raise ValueError("方案内容已变化，需要重新验证")
    state = _read_state()
    state[name].update({"state": "published", "published_at": __import__("datetime").datetime.now().isoformat(timespec="seconds")})
    _write_state(state)
    return metadata(name)


def delete_scheme(name: str) -> bool:
    """删除用户方案（内置方案保护）。"""
    name = _safe_name(name)
    if name in BUILTIN_SCHEMES:
        raise ValueError(f"内置方案 '{name}' 受保护，不可删除")
    path = custom_dir() / f"{name}.yaml"
    if not path.exists():
        return False
    path.unlink()
    # 清理状态 + 版本
    state = _read_state()
    state.pop(name, None)
    _write_state(state)
    vf = _version_file(name)
    if vf.exists():
        vf.unlink()
    return True


def list_scheme_stores() -> list[dict]:
    """列出用户方案（含启停状态/默认），供前端管理页。"""
    state = _read_state()
    out = []
    for p in sorted(custom_dir().glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            name = data.get("name", p.stem)
        except Exception:
            continue
        if p.stem.startswith("_"):
            continue
        s = state.get(name, {})
        out.append({
            "name": name,
            "version": str(data.get("version", "1.0")),
            "description": data.get("description", ""),
            "applicable_types": list(data.get("applicable_types", ["A", "B", "C", "D"])),
            "enabled": bool(s.get("enabled", True)),
            "default": bool(s.get("default", False)),
            "is_builtin": False,
            "source": str(p),
            "state": s.get("state", "published"),
            "validated_at": s.get("validated_at"),
            "published_at": s.get("published_at"),
        })
    return out


def is_enabled(name: str) -> bool:
    state = _read_state()
    return bool(state.get(name, {}).get("enabled", True))


def set_enabled(name: str, enabled: bool) -> None:
    name = _safe_name(name)
    if name in BUILTIN_SCHEMES:
        raise ValueError(f"内置方案 '{name}' 受保护，不可启停")
    state = _read_state()
    entry = state.setdefault(name, {})
    entry["enabled"] = bool(enabled)
    _write_state(state)


def set_default(name: str) -> None:
    """设为默认：清空其余默认后置当前。"""
    name = _safe_name(name)
    if name in BUILTIN_SCHEMES:
        raise ValueError(f"内置方案 '{name}' 受保护，不可设为用户默认")
    state = _read_state()
    for n, e in state.items():
        e["default"] = (n == name)
    state.setdefault(name, {})["default"] = True
    _write_state(state)


def is_default(name: str) -> bool:
    name = _safe_name(name)
    state = _read_state()
    return bool(state.get(name, {}).get("default", False))


def clone_scheme(src_name: str, new_name: str) -> dict:
    """从现有方案复制（FR-2.5 新建入口之一）。"""
    from StockInvestmentTool.portfolio.settings import read_scheme

    content = read_scheme(src_name)
    data = yaml.safe_load(content)
    data["name"] = new_name
    data["version"] = "1.0"
    data["description"] = f"(克隆自 {src_name}) {data.get('description', '')}"
    import yaml as _yaml
    new_content = _yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return save_scheme(new_name, new_content)
