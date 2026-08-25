# -*- coding: utf-8 -*-
"""策略编排器 — 结构化模型 ↔ 方案 YAML 互转 + 校验（FR-2.3）

设计意图（HLD §4.1 / ADR-1 / ADR-4）：
  - 唯一转换点：前端拼的「结构化方案模型」↔ 落盘 YAML，都经此模块，
    避免前端拼 YAML 字符串；
  - 生成 YAML 用 `yaml.safe_dump`，加载校验用 `yaml.safe_load` + `SchemeConfig`；
  - 校验失败抛 ValueError（含明确错误信息），由路由返回给前端提示。

模型（前端表单 → 本模块）约定：
  {
    "name": str,
    "version": str,
    "description": str,
    "applicable_types": [str],
    "buy_rules": [{ "type", "params": {} }],
    "sell_rules": [{ "type", "params": {} }],
    "risk": { "drawdown_stop", "min_profit_for_dd", "technical_stop_enabled",
              "stop_loss_by_type": {} },
    "backtest": { "initial_cash", "optimize", "grid_search": {trail_thresholds, buy_offsets} },
    "strategy_spec": {}
  }
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def model_to_yaml(model: dict) -> str:
    """结构化模型 → 方案 YAML 字符串（含校验）。

    Raises:
        ValueError: 模型结构非法或必填缺失。
    """
    import yaml

    if not isinstance(model, dict):
        raise ValueError("方案模型必须是映射")

    name = (model.get("name") or "").strip()
    if not name:
        raise ValueError("方案缺少必填字段: name")

    doc = _build_doc(model)
    try:
        return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                              default_flow_style=False)
    except yaml.YAMLError as e:
        raise ValueError(f"方案序列化失败: {e}") from e


def model_to_config(model: dict):
    """结构化模型 → 校验通过的 SchemeConfig。

    若校验失败抛 ValueError。
    """
    from StockInvestmentTool.core.scheme import load_scheme_from_dict

    doc = _build_doc(model)
    return load_scheme_from_dict(doc)


def _build_doc(model: dict) -> dict:
    """把模型规整为标准方案 dict（供 safe_dump / load_scheme_from_dict）。"""
    name = (model.get("name") or "").strip()
    if not name:
        raise ValueError("方案缺少必填字段: name")

    doc: dict[str, Any] = {
        "name": name,
        "version": str(model.get("version") or "1.0"),
        "description": model.get("description", ""),
        "applicable_types": list(model.get("applicable_types") or ["A", "B", "C", "D"]),
    }

    if model.get("strategy_spec"):
        doc["strategy_spec"] = model["strategy_spec"]

    # 买卖规则：type + params（params 为嵌套结构，逐条校验）
    for kind in ("buy_rules", "sell_rules"):
        rules = model.get(kind) or []
        out = []
        for r in rules:
            if not isinstance(r, dict):
                raise ValueError(f"{kind} 中某规则不是映射结构")
            rtype = (r.get("type") or "").strip()
            if not rtype:
                raise ValueError(f"{kind} 中某规则缺少 type")
            out.append({"type": rtype, "params": r.get("params") or {}})
        doc[kind] = out

    # 风控
    risk = model.get("risk") or {}
    doc["risk"] = {
        "stop_loss_by_type": risk.get("stop_loss_by_type") or {},
        "volume_surge_threshold": float(risk.get("volume_surge_threshold", 1.8)),
        "technical_stop_enabled": bool(risk.get("technical_stop_enabled", True)),
        "drawdown_stop": float(risk.get("drawdown_stop", 0.08)),
        "min_profit_for_dd": float(risk.get("min_profit_for_dd", 0.06)),
    }

    # 回测
    bt = model.get("backtest") or {}
    gs = bt.get("grid_search") or {}
    bt_doc: dict[str, Any] = {
        "initial_cash": float(bt.get("initial_cash", 100_000)),
        "optimize": bool(bt.get("optimize", True)),
    }
    if gs:
        bt_doc["grid_search"] = {
            "trail_thresholds": [float(x) for x in gs.get("trail_thresholds", [])],
            "buy_offsets": [float(x) for x in gs.get("buy_offsets", [])],
        }
    doc["backtest"] = bt_doc
    return doc


def yaml_to_model(content: str) -> dict:
    """加载既有方案 YAML → 结构化模型（供前端编辑回填）。

    解析失败抛 ValueError。schema 字段不还原（由 rule_registry 提供）。
    """
    import yaml

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError(f"方案 YAML 解析失败: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("方案 YAML 顶层必须是映射")

    return {
        "name": data.get("name", ""),
        "version": str(data.get("version", "1.0")),
        "description": data.get("description", ""),
        "applicable_types": list(data.get("applicable_types", ["A", "B", "C", "D"])),
        "strategy_spec": data.get("strategy_spec") or {},
        "buy_rules": data.get("buy_rules") or [],
        "sell_rules": data.get("sell_rules") or [],
        "risk": data.get("risk") or {},
        "backtest": data.get("backtest") or {},
    }


def validate_yaml(content: str) -> dict:
    """校验一段 YAML 字符串能否作为合法方案。

    Returns:
        {"ok": True} 或 {"ok": False, "error": "..."}
    """
    try:
        model = yaml_to_model(content)
        model_to_config(model)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _empty_model() -> dict:
    """空模板模型（FR-2.5 新建入口）。"""
    return {
        "name": "new_scheme",
        "version": "1.0",
        "description": "新建策略方案",
        "applicable_types": ["A", "B", "C", "D", "E"],
        "strategy_spec": {},
        "buy_rules": [],
        "sell_rules": [],
        "risk": {"drawdown_stop": 0.08, "min_profit_for_dd": 0.06,
                 "technical_stop_enabled": True, "stop_loss_by_type": {},
                 "volume_surge_threshold": 1.8},
        "backtest": {"initial_cash": 100_000, "optimize": True, "grid_search": {}},
    }
