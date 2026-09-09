"""Declarative dataset definitions loaded from YAML."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml


DEFAULT_DATASET_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "datasets"


class DatasetConfigError(ValueError):
    """Raised when a dataset definition is missing or unsafe to load."""


def _require_mapping(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise DatasetConfigError(f"{name} 必须是对象")
    return value


def _require_list(value: Any, name: str) -> list:
    if not isinstance(value, list):
        raise DatasetConfigError(f"{name} 必须是列表")
    return value


def load_dataset_config(dataset_name: str = "stock_daily", path: Path | str | None = None) -> dict:
    config_path = Path(path) if path else DEFAULT_DATASET_CONFIG_DIR / f"{dataset_name}.yaml"
    if not config_path.exists():
        raise DatasetConfigError(f"数据集配置不存在: {config_path}")
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise DatasetConfigError(f"数据集配置 YAML 无法解析: {config_path}") from exc
    validate_dataset_config(data, dataset_name=dataset_name)
    data["_config_path"] = str(config_path)
    data["_config_checksum"] = hashlib.sha256(
        config_path.read_bytes()
    ).hexdigest()
    return data


def validate_dataset_config(data: dict, *, dataset_name: str = "stock_daily") -> None:
    root = _require_mapping(data, "配置根节点")
    dataset = _require_mapping(root.get("dataset"), "dataset")
    if dataset.get("name") != dataset_name:
        raise DatasetConfigError(f"dataset.name 必须为 {dataset_name}")
    for key in ("display_name", "description", "grain", "schema_version", "update_frequency"):
        if not isinstance(dataset.get(key), str) or not dataset[key].strip():
            raise DatasetConfigError(f"dataset.{key} 必须是非空字符串")
    primary_keys = _require_list(dataset.get("primary_keys"), "dataset.primary_keys")
    if not primary_keys or any(not isinstance(item, str) or not item for item in primary_keys):
        raise DatasetConfigError("dataset.primary_keys 必须是非空字符串列表")
    partition = _require_mapping(dataset.get("partition"), "dataset.partition")
    if partition.get("type") not in {"month", "snapshot", "symbol"} or not isinstance(partition.get("path"), str):
        raise DatasetConfigError("dataset.partition 必须声明合法类型（month/snapshot/symbol）和 path")

    fields = _require_list(root.get("fields"), "fields")
    names = []
    for index, field in enumerate(fields):
        field = _require_mapping(field, f"fields[{index}]")
        for key in ("name", "display_name", "data_type"):
            if not isinstance(field.get(key), str) or not field[key].strip():
                raise DatasetConfigError(f"fields[{index}].{key} 必须是非空字符串")
        if not isinstance(field.get("nullable"), bool):
            raise DatasetConfigError(f"fields[{index}].nullable 必须是布尔值")
        names.append(field["name"])
    if len(names) != len(set(names)) or any(key not in names for key in primary_keys):
        raise DatasetConfigError("字段名称必须唯一且覆盖所有主键")
    if {field["name"] for field in fields if field.get("primary_key")} != set(primary_keys):
        raise DatasetConfigError("字段 primary_key 标记必须与 dataset.primary_keys 一致")

    sources = _require_list(root.get("sources"), "sources")
    source_names = []
    for index, source in enumerate(sources):
        source = _require_mapping(source, f"sources[{index}]")
        for key in ("name", "role"):
            if not isinstance(source.get(key), str) or not source[key].strip():
                raise DatasetConfigError(f"sources[{index}].{key} 必须是非空字符串")
        if not isinstance(source.get("priority"), int):
            raise DatasetConfigError(f"sources[{index}].priority 必须是整数")
        _require_mapping(source.get("field_mapping", {}), f"sources[{index}].field_mapping")
        _require_mapping(source.get("unit_conversions", {}), f"sources[{index}].unit_conversions")
        rules = source.get("unit_rules", [])
        if not isinstance(rules, list):
            raise DatasetConfigError(f"sources[{index}].unit_rules 必须是列表")
        defaults = 0
        for rule_index, rule in enumerate(rules):
            rule = _require_mapping(rule, f"sources[{index}].unit_rules[{rule_index}]")
            if not isinstance(rule.get("name"), str) or not rule["name"].strip():
                raise DatasetConfigError(f"sources[{index}].unit_rules[{rule_index}].name 必须是非空字符串")
            if not isinstance(rule.get("volume"), str) or not isinstance(rule.get("amount"), str):
                raise DatasetConfigError(f"sources[{index}].unit_rules[{rule_index}] 必须声明 volume 和 amount")
            if rule.get("default"):
                defaults += 1
            elif not isinstance(rule.get("code_prefixes"), list) or not rule["code_prefixes"]:
                raise DatasetConfigError(f"sources[{index}].unit_rules[{rule_index}] 必须声明 code_prefixes 或 default")
        if defaults > 1:
            raise DatasetConfigError(f"sources[{index}].unit_rules 最多只能有一个 default 规则")
        source_names.append(source["name"])
    if len(source_names) != len(set(source_names)) or not sources:
        raise DatasetConfigError("sources 必须非空且来源名称唯一")

    consumers = _require_list(root.get("consumers"), "consumers")
    for index, consumer in enumerate(consumers):
        consumer = _require_mapping(consumer, f"consumers[{index}]")
        for key in ("name", "type", "purpose", "required_quality", "fallback_policy"):
            if not isinstance(consumer.get(key), str) or not consumer[key].strip():
                raise DatasetConfigError(f"consumers[{index}].{key} 必须是非空字符串")

    quality = _require_mapping(root.get("quality"), "quality")
    if not isinstance(quality.get("publish_warning"), bool):
        raise DatasetConfigError("quality.publish_warning 必须是布尔值")


def config_fingerprint(data: dict) -> str:
    """Return a stable fingerprint excluding loader bookkeeping fields."""
    payload = {key: value for key, value in data.items() if not key.startswith("_")}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
