from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_data_center_uses_user_data_item_language():
    content = (ROOT / "web/templates/data_center.html").read_text(encoding="utf-8")
    assert "数据资产" in content
    assert "最新实际日期" in content
    assert "股票类型覆盖" not in content
    assert "Raw Batch" not in content
    assert "Candidate" not in content


def test_task_center_has_explicit_actions_and_period_language():
    content = (ROOT / "web/templates/task_center.html").read_text(encoding="utf-8")
    assert "任务列表" in content
    assert "执行周期" in content
    assert "立即执行" in content
    assert "配置版本" in content
    assert "数据总览" in content
