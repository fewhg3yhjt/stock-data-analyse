from StockInvestmentTool.web.scheduler import _task_schedule_enabled


def test_auxiliary_tasks_follow_schedule_configuration():
    configured = {
        "money_flow_capture": {"schedule": {"enabled": True}},
        "fundamentals_capture": {"schedule": {"enabled": False}},
    }
    assert _task_schedule_enabled(configured, "money_flow_capture") is True
    assert _task_schedule_enabled(configured, "fundamentals_capture") is False
    assert _task_schedule_enabled(configured, "valuation_capture") is False
