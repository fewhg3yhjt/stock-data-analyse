"""核心页面公共导航回归测试。"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def client():
    from StockInvestmentTool.web.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["admin"] = True
    return client


@pytest.mark.parametrize(
    ("path", "active"),
    [
        ("/", "workbench"),
        ("/strategy-simulation", "simulation"),
        ("/market", "market"),
        ("/dashboard/observe", "observe"),
        ("/watchlist", "watchlist"),
        ("/strategy", "strategy"),
        ("/indicator-center", "indicators"),
        ("/strategy-composer", "composer"),
        ("/notify-center", "notify"),
        ("/dashboard/warroom", "warroom"),
        ("/dashboard/review", "review"),
        ("/settings", "settings"),
        ("/data-center", "data"),
        ("/data-center/assets", "data"),
        ("/data-center/tasks", "data"),
        ("/watch-pool", "watchpool"),
        ("/research", "research"),
        ("/system", "system"),
        ("/market-discovery", "market"),
        ("/operation-points", "research"),
    ],
)
def test_core_page_renders_shared_navigation(client, path, active):
    response = client.get(path)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    if path in ("/data-center", "/data-center/assets", "/data-center/tasks"):
        assert 'class="dm-sidebar"' in html
        assert 'class="dm-tabs"' in html
        assert "任务中心" in html
        return
    assert 'class="nav"' in html
    assert 'href="/workbench"' in html
    assert 'href="/dashboard/warroom"' in html
    assert f'<a href="{_href_for_active(active)}" class="active">' in html


def test_watch_pool_contains_expandable_kline_detail(client):
    response = client.get("/watch-pool")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "/static/stock-detail.js" in html
    assert "展开K线" in html
    assert "StockDetail.load" in Path(__file__).parents[1].joinpath("web/static/watch-pool.js").read_text()
    assert "选择策略模拟" in html
    assert "运行策略模拟" in html


def test_research_and_system_restore_core_links(client):
    research = client.get("/research").get_data(as_text=True)
    assert 'href="/indicator-center"' in research
    assert 'href="/strategy-composer"' in research
    assert 'href="/strategy"' in research
    system = client.get("/system").get_data(as_text=True)
    assert 'href="/data-center"' in system
    assert 'href="/notify-center"' in system
    assert 'href="/settings"' in system


def test_market_discovery_page_links_workflow(client):
    html = client.get("/market-discovery").get_data(as_text=True)
    assert "开始筛选" in html
    assert "/api/market-discovery/stocks" in html
    assert "板块资金流作为后续待办" in html
    assert "符合条件总数" in html
    assert "展开K线" in html


def test_market_discovery_options_api(client):
    response = client.get("/api/market-discovery/options")
    assert response.status_code == 200
    assert response.get_json()["status"] == "success"


def test_workbench_exposes_market_discovery(client):
    html = client.get("/workbench").get_data(as_text=True)
    assert 'href="/market-discovery"' in html
    assert "发现个股" in html
    assert 'href="/operation-points"' in html


def test_home_is_workbench_and_strategy_simulation_is_form(client):
    home = client.get("/")
    assert home.status_code == 200
    assert "完整投资流程" in home.get_data(as_text=True)
    simulation = client.get("/strategy-simulation?code=sh600900&name=长江电力&stock_type=A&scheme=right_aggressive_growth")
    assert simulation.status_code == 200
    html = simulation.get_data(as_text=True)
    assert "策略模拟参数" in html
    assert 'value="sh600900"' in html
    assert 'value="长江电力"' in html
    assert "策略模拟" in html


def test_strategy_simulation_marks_visible_research_navigation_active(client):
    html = client.get("/strategy-simulation").get_data(as_text=True)

    primary = html.split('<div class="nav-secondary">', 1)[0]
    assert '<a href="/research" class="active">研究</a>' in primary
    assert '<a href="/strategy-simulation" class="active">策略模拟</a>' in html


def test_workbench_data_status_uses_safe_status_classes(client):
    html = client.get("/workbench").get_data(as_text=True)

    assert "const dataStatuses=new Set" in html
    assert "status-'+normalized" in html
    assert "setDataStatus('failed')" in html

    css = Path(__file__).parents[1].joinpath("web/static/base.css").read_text()
    assert ".tag.status-running" in css


def test_stock_link_redirects_to_strategy_simulation(client):
    response = client.get("/?code=sh600900&name=长江电力")
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/strategy-simulation?")


def test_removed_legacy_analysis_and_simulation_pages(client):
    assert client.get("/analyze").status_code == 404
    assert client.get("/simulation").status_code == 404


def test_removed_task_center_route_is_not_available(client):
    assert client.get("/task-center").status_code == 404


def _href_for_active(active: str) -> str:
    return {
        "workbench": "/workbench",
        "market": "/market",
        "observe": "/dashboard/observe",
        "watchlist": "/watchlist",
        "simulation": "/strategy-simulation",
        "strategy": "/research",
        "indicators": "/research",
        "composer": "/research",
        "notify": "/system",
        "warroom": "/dashboard/warroom",
        "review": "/dashboard/review",
        "settings": "/system",
        "data": "/system",
        "watchpool": "/watch-pool",
        "research": "/research",
        "system": "/system",
    }[active]
