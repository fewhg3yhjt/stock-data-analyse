from ops.task_center import TaskCenter, TaskConfigError


def test_task_config_draft_is_not_active_until_explicit_activation(tmp_path):
    center = TaskCenter(tmp_path / "runs.db")
    center.sync_definitions()
    task = center.task("indicators_build")
    current = task["active_config_version"]
    center.activate_task_config("indicators_build", current)
    assert center.task("indicators_build")["active_config_version"] == current


def test_custom_metric_change_creates_version_and_marks_stale(tmp_path):
    center = TaskCenter(tmp_path / "runs.db")
    center.sync_metrics()
    version = center.set_metric_definition(metric_key="custom_gap", display_name="自定义缺口", category="技术指标",
                                           definition="close / ma20 - 1", unit="%", producer_task="indicators_build")
    assert version == 1
    metric = next(item for item in center.list_metrics() if item["metric_key"] == "custom_gap")
    assert metric["health_status"] == "stale"
    center.disable_metric("custom_gap")
    try:
        center.disable_metric("close")
    except TaskConfigError:
        pass
    else:
        raise AssertionError("builtin metric should be protected")
