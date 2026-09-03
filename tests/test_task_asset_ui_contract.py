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


def test_market_page_rotation_contract():
    template = (ROOT / "web/templates/market.html").read_text(encoding="utf-8")
    script = (ROOT / "web/static/market.js").read_text(encoding="utf-8")

    assert "window.MARKET_ROTATIONS" in template
    rotation_markup = template.split('<div class="ui-card rotation-card">', 1)[1].split(
        '<div class="ui-card"><div class="ui-card-header"><span><i class="fas fa-sitemap"', 1
    )[0]
    assert '<tbody>{% for' not in rotation_markup
    assert "排名（按5日收益）" in script
    assert "data-filter=\"${filterKey}\"" in script
    assert "fa-arrow-down" in script
    assert "fa-arrow-up" in script
    assert "sortTable(defaultSort" not in script
    for field in (
        "rank_5d", "as_of", "industry_name", "status", "score", "return_1d",
        "return_5d", "return_20d", "up_ratio", "amount_ratio", "member_count",
    ):
        assert field in script
    assert "看走势/展开K线" in script
    assert "筛选板块内股票" in script
    assert "StockChart.drawLine" in script
    assert "tooltip:" not in script
    assert "dataZoom" not in script


def test_market_discovery_conditions_does_not_read_removed_board_field():
    source = (ROOT / "web/templates/market_discovery.html").read_text(encoding="utf-8")
    assert "board:'ALL'" in source
    assert "board:document.getElementById('board').value" not in source
