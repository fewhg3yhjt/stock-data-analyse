"""核心页面公共导航回归测试。"""

from __future__ import annotations

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
        ("/", "analyze"),
        ("/market", "market"),
        ("/dashboard/observe", "observe"),
        ("/watchlist", "watchlist"),
        ("/simulation", "simulation"),
        ("/strategy", "strategy"),
        ("/indicator-center", "indicators"),
        ("/strategy-composer", "composer"),
        ("/notify-center", "notify"),
        ("/dashboard/warroom", "warroom"),
        ("/dashboard/review", "review"),
        ("/settings", "settings"),
        ("/data-center", "data"),
        ("/watch-pool", "watchpool"),
        ("/research", "research"),
        ("/system", "system"),
        ("/market-discovery", "market"),
    ],
)
def test_core_page_renders_shared_navigation(client, path, active):
    response = client.get(path)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'class="nav"' in html
    assert 'href="/"' in html
    assert 'href="/dashboard/warroom"' in html
    assert f'<a href="{_href_for_active(active)}" class="active">' in html


def test_watch_pool_contains_expandable_kline_detail(client):
    response = client.get("/watch-pool")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "/static/stock-detail.js" in html
    assert "展开K线" in html
    assert "StockDetail.load" in html
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
    assert "执行本地筛选" in html
    assert "/api/market-discovery/stocks" in html
    assert "板块资金流作为后续待办" in html
    assert "符合条件总数" in html
    assert "展开K线" in html


def test_workbench_exposes_market_discovery(client):
    html = client.get("/workbench").get_data(as_text=True)
    assert 'href="/market-discovery"' in html
    assert "发现个股" in html


def _href_for_active(active: str) -> str:
    return {
        "analyze": "/",
        "market": "/market",
        "observe": "/dashboard/observe",
        "watchlist": "/watchlist",
        "simulation": "/simulation",
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
