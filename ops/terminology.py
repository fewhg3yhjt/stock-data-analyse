"""Shared user-facing terminology mappings for task and data APIs."""

STAGE_LABELS = {
    "CAPTURE": "数据采集",
    "BUILD": "数据构建",
    "QUALITY": "数据质量",
    "PUBLISH": "数据发布",
    "DERIVED": "派生计算",
    "MAINTENANCE": "数据维护",
}

TASK_TYPE_LABELS = {
    "SOURCE_CAPTURE": "源数据采集",
    "DATA_BUILD": "标准数据构建",
    "QUALITY_CHECK": "数据质量检查",
    "DATA_PUBLISH": "数据正式发布",
    "INDICATOR_BUILD": "技术指标计算",
    "FACTOR_BUILD": "历史因子归档（已废弃新生产）",
    "DERIVED_BUILD": "派生数据计算",
    "LEGACY_CONVERT": "历史数据转换",
    "DATA_REPAIR": "数据修复",
}

STATUS_LABELS = {
    "requested": "已请求", "scheduled": "已排期", "waiting": "等待执行",
    "running": "执行中", "success": "已完成", "partial_success": "部分完成",
    "skipped": "已跳过", "failed": "执行失败", "cancelled": "已取消",
    "blocked": "已阻断", "healthy": "正常", "partial": "部分可用",
    "stale": "待更新", "critical": "异常", "empty": "暂无数据",
}

ARTIFACT_LABELS = {
    "raw_batch": "原始采集数据", "candidate": "待发布标准数据",
    "quality_report": "质量检查报告", "published_dataset": "正式数据",
    "indicator_output": "技术指标结果", "factor_output": "历史因子归档（已废弃新生产）",
    "diff_report": "新旧结果差异报告", "log": "执行日志",
}


def task_labels(task: dict) -> dict:
    return {**task, "stage_label": STAGE_LABELS.get(task.get("stage"), task.get("stage", "")),
            "task_type_label": TASK_TYPE_LABELS.get(task.get("task_type"), task.get("task_type", "")),
            "status_label": STATUS_LABELS.get(task.get("status"), task.get("status", ""))}


def artifact_labels(artifact: dict) -> dict:
    return {**artifact, "artifact_type_label": ARTIFACT_LABELS.get(
        artifact.get("artifact_type"), artifact.get("artifact_type", ""))}
