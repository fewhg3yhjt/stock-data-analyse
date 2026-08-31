from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_data_center_uses_user_data_item_language():
    content = (ROOT / "web/templates/data_center.html").read_text(encoding="utf-8")
    assert "数据资产" in content
    assert "最新实际日期" in content
    assert "股票类型覆盖" not in content
    assert "Raw Batch" not in content
    assert "Candidate" not in content
    assert "重点数据状态" in content
    assert "查看全部数据项" in content
    script = (ROOT / "web/static/data-center-adapter.js").read_text(encoding="utf-8")
    assert "loadOverview();" in script


def test_data_assets_is_a_distinct_catalog_page():
    content = (ROOT / "web/templates/data_center_assets.html").read_text(encoding="utf-8")
    assert "数据项目录" in content
    assert "全部分类" in content
    assert "重点数据状态" not in content


def test_task_center_has_explicit_actions_and_period_language():
    content = (ROOT / "web/templates/task_center.html").read_text(encoding="utf-8")
    script = (ROOT / "web/static/task-center.js").read_text(encoding="utf-8")
    assert "任务列表" in content
    assert "执行周期" in content
    assert "立即执行" in script
    assert "配置版本" in script
    assert "数据总览" in content
    assert "data.overview" in script
    assert "任务数据读取失败" in script
    assert "本次执行范围" in script
    assert "查看日志" in script
