from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_task_center_frontend_uses_new_task_endpoint():
    source = (ROOT / "web/static/task-center.js").read_text(encoding="utf-8")
    assert "/api/task-center/tasks/" in source
    assert "/api/data/jobs/" not in source
    assert "daily-sync" not in source
    assert "rebuild-indicators" not in source
    assert "rebuild-factors" not in source


def test_data_assets_page_loads_management_actions_after_inline_details():
    source = (ROOT / "web/templates/data_center_assets.html").read_text(encoding="utf-8")
    assert "/static/data-assets-admin.js?v=1" in source
    assert source.index("</script><script src=\"/static/data-assets-admin.js") > source.index("function openAsset")
    admin = (ROOT / "web/static/data-assets-admin.js").read_text(encoding="utf-8")
    assert "/definition" in admin
    assert "/disable" in admin
    assert "/regenerate" in admin
    assert "/publish" in admin
    assert "/rollback" in admin
