# -*- coding: utf-8 -*-
"""统一状态词表（阶段十一工作项 6）。

单一事实源：status → {label, severity, terminal}。
覆盖产品平面（biz Job Run、Simulation）与运维平面（Data Task、数据集）。

- label：用户可读中文
- severity：healthy / running / warning / error / unknown
- terminal：是否为终态（终态不可再转回 running）
"""

from __future__ import annotations

STATUS_META: dict[str, dict] = {
    # 业务请求/任务
    "requested": {"label": "已请求", "severity": "running", "terminal": False},
    "scheduled": {"label": "已排期", "severity": "running", "terminal": False},
    "waiting": {"label": "等待执行", "severity": "running", "terminal": False},
    "running": {"label": "执行中", "severity": "running", "terminal": False},
    "success": {"label": "已完成", "severity": "healthy", "terminal": True},
    "partial_success": {"label": "部分完成", "severity": "warning", "terminal": True},
    "skipped": {"label": "已跳过", "severity": "warning", "terminal": True},
    "failed": {"label": "执行失败", "severity": "error", "terminal": True},
    "cancelled": {"label": "已取消", "severity": "warning", "terminal": True},
    "publish_failed": {"label": "发布失败", "severity": "error", "terminal": True},
    "blocked": {"label": "已阻断", "severity": "warning", "terminal": False},
    # 模拟交易
    "PENDING": {"label": "待执行", "severity": "running", "terminal": False},
    "RUNNING": {"label": "执行中", "severity": "running", "terminal": False},
    "SUCCESS": {"label": "已完成", "severity": "healthy", "terminal": True},
    "FAILED": {"label": "执行失败", "severity": "error", "terminal": True},
    "CANCELLED": {"label": "已取消", "severity": "warning", "terminal": True},
    # 数据集/数据质量
    "healthy": {"label": "正常", "severity": "healthy", "terminal": False},
    "partial": {"label": "部分可用", "severity": "warning", "terminal": False},
    "stale": {"label": "待更新", "severity": "warning", "terminal": False},
    "critical": {"label": "异常", "severity": "error", "terminal": False},
    "empty": {"label": "暂无数据", "severity": "unknown", "terminal": False},
    "disabled": {"label": "已停用", "severity": "warning", "terminal": False},
    "unknown": {"label": "未知", "severity": "unknown", "terminal": False},
}


def status_meta(status: str) -> dict:
    """返回统一状态元数据；未知状态返回 unknown 兜底。"""
    return STATUS_META.get(status, STATUS_META["unknown"])


def status_info(status: str) -> dict:
    """统一输出 {code, label, severity, terminal}。"""
    meta = status_meta(status)
    return {"code": status, "label": meta["label"],
            "severity": meta["severity"], "terminal": meta["terminal"]}


def is_terminal(status: str) -> bool:
    return status_meta(status)["terminal"]


def decorate(item: dict) -> dict:
    """为状态字段追加统一元数据（不修改原 dict）。"""
    status = item.get("status") or item.get("state") or item.get("publish_status")
    if status is None:
        return item
    out = dict(item)
    out["status_info"] = status_info(status)
    out["status_label"] = status_meta(status)["label"]
    return out