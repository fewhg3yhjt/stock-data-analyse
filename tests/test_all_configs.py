# -*- coding: utf-8 -*-
"""全量配置解析与只读数据契约诊断测试（阶段一）。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from StockInvestmentTool.ops.task_center import TaskCenter, TaskConfigError, load_task_definitions
from StockInvestmentTool.warehouse.dataset_config import DatasetConfigError, load_dataset_config

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
DATASET_CONFIG_DIR = CONFIG_DIR / "datasets"
TASK_CONFIG_DIR = CONFIG_DIR / "tasks"


def _dataset_yamls() -> list[Path]:
    return sorted(DATASET_CONFIG_DIR.glob("*.yaml"))


def _task_yamls() -> list[Path]:
    return sorted(TASK_CONFIG_DIR.glob("*.yaml"))


def test_every_dataset_yaml_is_valid_yaml():
    for path in _dataset_yamls():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            pytest.fail(f"{path.name} YAML 无法解析: {exc}")
        assert isinstance(data, dict), f"{path.name} 根节点不是对象"


def test_every_dataset_yaml_loads_through_config_loader():
    for path in _dataset_yamls():
        name = path.stem
        config = load_dataset_config(name)
        assert config["dataset"]["name"] == name, f"{path.name} dataset.name 与文件名不一致"
        assert config["_config_path"] == str(path)
        assert len(config["_config_checksum"]) == 64


def test_new_dataset_yaml_automatically_enters_test_scope():
    expected = {path.stem for path in _dataset_yamls()}
    assert len(expected) >= 6, "配置目录应至少包含基础数据集定义"


def test_every_task_yaml_is_valid_yaml():
    for path in _task_yamls():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            pytest.fail(f"{path.name} YAML 无法解析: {exc}")
        assert isinstance(data, dict), f"{path.name} 根节点不是对象"


def test_load_task_definitions_parses_all_task_yamls():
    definitions = load_task_definitions()
    keys = {item["task"]["key"] for item in definitions}
    expected = {path.stem for path in _task_yamls()}
    assert keys == expected, f"任务定义与文件不一致: {keys ^ expected}"


def test_every_task_definition_passes_taskcenter_validation(tmp_path):
    center = TaskCenter(tmp_path / "management.db")
    for item in load_task_definitions():
        task_key = item["task"]["key"]
        try:
            center.validate_task_config(item)
        except TaskConfigError as exc:
            pytest.fail(f"任务 {task_key} 校验失败: {exc}")


def test_task_config_load_failure_reports_explicit_path(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("task:\n  key: [broken\n", encoding="utf-8")
    from StockInvestmentTool.ops.task_center import load_task_definitions
    with pytest.raises(TaskConfigError, match="bad.yaml"):
        load_task_definitions(tmp_path)


def test_dataset_config_load_failure_reports_explicit_path(tmp_path):
    bad = tmp_path / "broken.yaml"
    bad.write_text("dataset:\n  name: [x\n", encoding="utf-8")
    with pytest.raises(DatasetConfigError, match="broken.yaml"):
        load_dataset_config(path=bad)